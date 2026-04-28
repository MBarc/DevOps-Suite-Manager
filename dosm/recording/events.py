from __future__ import annotations

from dosm.recording.journal import PWD_PROMPT_RE, redact
from dosm.recording.state import get_active


# ---------------------------------------------------------------------------
# High-level event helpers — called from route hook points.
# Each function is a no-op when no recording is active for that user.
# ---------------------------------------------------------------------------

def record_pipeline_triggered(
    user_id: int, pipeline_name: str, run_id: int | None, provider: str
) -> None:
    rec = get_active(user_id)
    if rec is None or not rec.options.pipelines:
        return
    run_part = f" (run #{run_id})" if run_id else ""
    rec.writer.write_event(
        "Pipeline triggered", f"`{pipeline_name}` via {provider}{run_part}"
    )


def record_pipeline_status(
    user_id: int, pipeline_name: str, run_id: int | None, status: str
) -> None:
    rec = get_active(user_id)
    if rec is None or not rec.options.pipelines:
        return
    run_part = f" #{run_id}" if run_id else ""
    rec.writer.write_event(
        "Pipeline status", f"`{pipeline_name}`{run_part} → **{status}**"
    )


def record_host_open(
    user_id: int, host_name: str, protocol: str, credential_user: str | None
) -> None:
    rec = get_active(user_id)
    if rec is None or not rec.options.host_opens:
        return
    cred = f" as `{credential_user}`" if credential_user else ""
    rec.writer.write_event("Host opened", f"`{host_name}` ({protocol.upper()}){cred}")


def record_host_close(user_id: int, host_name: str) -> None:
    rec = get_active(user_id)
    if rec is None or not rec.options.host_opens:
        return
    rec.writer.write_event("Host closed", f"`{host_name}`")


def record_terminal_open(user_id: int, shell_name: str) -> None:
    rec = get_active(user_id)
    if rec is None or not (rec.options.commands or rec.options.terminal_output):
        return
    rec.writer.write_event("Terminal opened", f"`{shell_name}`")


def record_terminal_close(user_id: int, shell_name: str) -> None:
    rec = get_active(user_id)
    if rec is None or not (rec.options.commands or rec.options.terminal_output):
        return
    rec.writer.write_event("Terminal closed", f"`{shell_name}`")


def record_clipboard(user_id: int, direction: str, content: str) -> None:
    rec = get_active(user_id)
    if rec is None or not rec.options.clipboard:
        return
    if not content.strip():
        return
    display = content if len(content) <= 2000 else content[:2000] + "\n… [truncated]"
    rec.writer.write_block(f"Clipboard {direction}", "", display)


def record_plan_card_decision(
    user_id: int, tool: str, status: str, host: str | None, command: str | None
) -> None:
    rec = get_active(user_id)
    if rec is None or not rec.options.plan_cards:
        return
    details = f"`{tool}`"
    if host:
        details += f" on `{host}`"
    if command:
        details += f": `{redact(command)}`"
    rec.writer.write_event(f"Plan card {status}", details)


def record_plan_card_result(user_id: int, tool: str, ok: bool, summary: str) -> None:
    rec = get_active(user_id)
    if rec is None or not rec.options.plan_cards:
        return
    icon = "✓" if ok else "✗"
    rec.writer.write_event("Plan card result", f"`{tool}` {icon} {summary[:300]}")


def record_guac_command(
    user_id: int, protocol: str, host_name: str, line: str
) -> None:
    """Record a keystroke line captured from inside a Guacamole session.

    `protocol` is "ssh" or "rdp". `line` has already been redacted client-side
    when the time-gap heuristic flagged it as a likely password.
    """
    rec = get_active(user_id)
    if rec is None:
        return
    if protocol == "ssh" and not rec.options.guac_ssh_keystrokes:
        return
    if protocol == "rdp" and not rec.options.guac_rdp_keystrokes:
        return
    host_part = f"`{host_name}` " if host_name else ""
    rec.writer.write_event(
        f"Guac {protocol.upper()} keystroke", f"{host_part}$ `{line}`"
    )


def record_monitoring_query(user_id: int, source_name: str) -> None:
    rec = get_active(user_id)
    if rec is None or not rec.options.monitoring:
        return
    rec.writer.write_event("Monitoring queried", f"`{source_name}`")


# ---------------------------------------------------------------------------
# Per-terminal-session hook — instantiated inside the WebSocket handler.
# ---------------------------------------------------------------------------

class TerminalJournalHook:
    """Buffers input/output from one terminal session and writes events to the
    active recording's journal.  Created per WebSocket session; thread-safe
    through JournalWriter's internal lock."""

    # Flush buffered output once it crosses this threshold (bytes).
    _FLUSH_THRESHOLD = 4096

    def __init__(self, user_id: int, shell_name: str) -> None:
        self._uid = user_id
        self._shell = shell_name
        self._input_buf = ""
        self._last_output = ""    # rolling tail for password-prompt detection
        self._output_buf = ""
        self._output_bytes = 0
        self._output_capped = False
        self._pending_command: str | None = None

    def _rec(self):
        return get_active(self._uid)

    def on_input(self, data: str) -> None:
        rec = self._rec()
        if rec is None:
            return
        for ch in data:
            if ch in ("\r", "\n"):
                line = self._input_buf
                self._input_buf = ""
                if not line.strip():
                    continue
                after_pwd = bool(PWD_PROMPT_RE.search(self._last_output))
                clean = redact(line, after_password_prompt=after_pwd)
                # Flush the previous command's output before logging the new command.
                self._flush_pending_output(rec)
                self._pending_command = clean
                self._output_capped = False
            elif ch in ("\x7f", "\x08"):
                self._input_buf = self._input_buf[:-1]
            else:
                self._input_buf += ch

    def on_output(self, data: str) -> None:
        self._last_output = (self._last_output + data)[-512:]
        rec = self._rec()
        if rec is None or not rec.options.terminal_output or self._output_capped:
            return
        cap = rec.options.output_cap_kb * 1024 if rec.options.output_cap_kb > 0 else None
        encoded_len = len(data.encode("utf-8", errors="replace"))
        if cap is not None and (self._output_bytes + encoded_len) > cap:
            # Write up to cap, then mark capped.
            remaining = max(0, cap - self._output_bytes)
            snippet = data.encode("utf-8", errors="replace")[:remaining].decode(
                "utf-8", errors="replace"
            )
            overflow = encoded_len - remaining
            self._output_buf += snippet
            self._output_bytes += remaining
            self._output_capped = True
            self._flush_pending_output(
                rec,
                cap_note=f"*[truncated — {overflow} B beyond {rec.options.output_cap_kb} KB cap]*",
            )
        else:
            self._output_buf += data
            self._output_bytes += encoded_len
            # Periodic flush so large outputs don't all land at session close.
            if self._output_bytes >= self._FLUSH_THRESHOLD:
                self._flush_pending_output(rec)

    def flush(self) -> None:
        """Call at session close to drain any buffered data."""
        rec = self._rec()
        self._flush_pending_output(rec)

    def _flush_pending_output(
        self, rec, *, cap_note: str = ""
    ) -> None:
        if rec is None:
            self._pending_command = None
            self._output_buf = ""
            self._output_bytes = 0
            return

        cmd = self._pending_command
        out = self._output_buf.strip()

        if cmd is not None and rec.options.commands:
            label = f"`{self._shell}` $ `{cmd}`"
            if cap_note:
                label += f"  {cap_note}"
            if out and rec.options.terminal_output:
                rec.writer.write_block("Command + output", label, self._output_buf.rstrip())
            else:
                rec.writer.write_event("Command", label)
        elif out and rec.options.terminal_output:
            label = f"`{self._shell}`"
            if cap_note:
                label += f"  {cap_note}"
            rec.writer.write_block("Output", label, self._output_buf.rstrip())

        self._pending_command = None
        self._output_buf = ""
        self._output_bytes = 0
