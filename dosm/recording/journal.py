from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass
class RecordingOptions:
    timestamps: bool = True
    pipelines: bool = True
    host_opens: bool = True
    commands: bool = True
    terminal_output: bool = True
    clipboard: bool = True
    plan_cards: bool = True
    monitoring: bool = True
    navigation: bool = False
    output_cap_kb: int = 10  # 0 = no cap
    # Keystroke capture inside Guacamole sessions (covers both SSH and RDP).
    # Off by default because there is no output stream to detect password
    # prompts against — the time-gap heuristic is used instead, but it is
    # imperfect.
    guac_keystrokes: bool = False

    def to_dict(self) -> dict:
        return {
            "timestamps": self.timestamps,
            "pipelines": self.pipelines,
            "host_opens": self.host_opens,
            "commands": self.commands,
            "terminal_output": self.terminal_output,
            "clipboard": self.clipboard,
            "plan_cards": self.plan_cards,
            "monitoring": self.monitoring,
            "navigation": self.navigation,
            "output_cap_kb": self.output_cap_kb,
            "guac_keystrokes": self.guac_keystrokes,
        }

    @classmethod
    def from_dict(cls, d: dict) -> RecordingOptions:
        return cls(
            timestamps=bool(d.get("timestamps", True)),
            pipelines=bool(d.get("pipelines", True)),
            host_opens=bool(d.get("host_opens", True)),
            commands=bool(d.get("commands", True)),
            terminal_output=bool(d.get("terminal_output", True)),
            clipboard=bool(d.get("clipboard", True)),
            plan_cards=bool(d.get("plan_cards", True)),
            monitoring=bool(d.get("monitoring", True)),
            navigation=bool(d.get("navigation", False)),
            output_cap_kb=int(d.get("output_cap_kb", 10)),
            guac_keystrokes=bool(d.get("guac_keystrokes", False)),
        )


# Regex patterns used for password-prompt detection and secret redaction.
PWD_PROMPT_RE = re.compile(
    r'(?i)(?:password|passcode|passphrase|\[sudo\] password for \S+|enter password):?\s*$'
)
_CLI_SECRET_FLAGS = re.compile(
    r'(?i)((?:--password|--passwd|--token|--secret|--api[-_]?key|--access[-_]?key)[= ])(\S+)'
)
_BEARER = re.compile(r'(?i)(Authorization:\s*Bearer\s+)(\S+)')
_JWT = re.compile(r'eyJ[A-Za-z0-9_\-]+\.eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+')
_PEM = re.compile(r'-----BEGIN [A-Z ]+-----')
_AWS_KEY = re.compile(r'\bAKIA[0-9A-Z]{16}\b')


def _looks_like_secret(text: str) -> bool:
    return bool(_JWT.search(text) or _PEM.search(text) or _AWS_KEY.search(text))


def redact(line: str, *, after_password_prompt: bool = False) -> str:
    """Apply best-effort secret redaction to a single line of text."""
    if after_password_prompt:
        return "******"
    line = _CLI_SECRET_FLAGS.sub(r'\g<1>******', line)
    line = _BEARER.sub(r'\g<1>******', line)
    if _looks_like_secret(line):
        return "[redacted — potential secret]"
    return line


class JournalWriter:
    """Append-only markdown journal file for one recording session."""

    def __init__(self, path: Path, slug: str, username: str, options: RecordingOptions):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._opts = options
        self._lock = threading.Lock()
        fh = open(path, "w", encoding="utf-8", newline="\n")
        ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        fh.write(f"# Session Journal: {slug}\n\n")
        fh.write(f"- **User:** {username}\n")
        fh.write(f"- **Started:** {ts}\n\n")
        fh.write("---\n\n")
        fh.flush()
        self._fh = fh

    @property
    def path(self) -> Path:
        return self._path

    @property
    def options(self) -> RecordingOptions:
        return self._opts

    @options.setter
    def options(self, val: RecordingOptions) -> None:
        with self._lock:
            self._opts = val

    def write_event(self, category: str, text: str) -> None:
        with self._lock:
            if self._fh is None:
                return
            if self._opts.timestamps:
                ts = time.strftime("%H:%M:%S")
                header = f"**[{ts}] {category}**"
            else:
                header = f"**{category}**"
            self._fh.write(f"{header} — {text}\n\n")
            self._fh.flush()

    def write_block(self, category: str, label: str, content: str) -> None:
        """Write an event with a fenced code block for multi-line content."""
        with self._lock:
            if self._fh is None:
                return
            if self._opts.timestamps:
                ts = time.strftime("%H:%M:%S")
                header = f"**[{ts}] {category}**"
            else:
                header = f"**{category}**"
            label_part = f" — {label}" if label else ""
            self._fh.write(f"{header}{label_part}\n\n```\n{content}\n```\n\n")
            self._fh.flush()

    def write_footer(self, status: str) -> None:
        with self._lock:
            if self._fh is None:
                return
            ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
            self._fh.write(f"---\n\n*Recording {status}: {ts}*\n")
            self._fh.flush()

    def close(self) -> None:
        with self._lock:
            if self._fh is None:
                return
            try:
                self._fh.close()
            finally:
                self._fh = None
