from __future__ import annotations

import fnmatch
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from dosm.config import Config


@dataclass
class ActionResult:
    """Standardized outcome shape from any agent tool invocation."""

    ok: bool
    summary: str
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    duration_ms: int | None = None
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


@dataclass
class ActionSpec:
    """Metadata describing a tool the agent may propose.

    `runner` is an async callable: `await runner(cfg, args, *, db_url) -> ActionResult`.
    The args dict is the *effective* args (post-Edit), already validated.
    `args_schema` is a list of {name, type, required, description} entries
    used to render the LLM's prompt and the Edit form.
    """

    name: str
    description: str
    args_schema: list[dict]
    runner: Callable[..., Awaitable[ActionResult]]
    classify: Callable[[dict], str] = lambda args: "safe"


_REGISTRY: dict[str, ActionSpec] = {}


def register_action(spec: ActionSpec) -> None:
    _REGISTRY[spec.name] = spec


def list_actions() -> list[ActionSpec]:
    return list(_REGISTRY.values())


def get_action(name: str) -> ActionSpec | None:
    return _REGISTRY.get(name)


def classify_command(cfg: Config, command: str) -> str:
    """`safe` if `command` matches one of the allow-list globs, else `elevated`."""
    cmd = command.strip()
    if not cmd:
        return "elevated"
    for pattern in cfg.ssh_command_policy.allow_list:
        if fnmatch.fnmatch(cmd, pattern):
            return "safe"
    return "elevated"


# ---- ssh_exec ------------------------------------------------------------


async def _ssh_exec_runner(cfg: Config, args: dict) -> ActionResult:
    import asyncio
    import time

    import asyncssh  # type: ignore
    from sqlalchemy import select

    from dosm.db import session_scope
    from dosm.models import Credential, Host
    from dosm.secrets import SecretNotFound, get_backend

    host_id = args.get("host_id")
    host_name = args.get("host")
    command = (args.get("command") or "").strip()
    timeout = float(args.get("timeout") or 30.0)

    if not command:
        return ActionResult(ok=False, summary="empty command")

    with session_scope() as s:
        host: Host | None = None
        if host_id is not None:
            host = s.get(Host, int(host_id))
        elif host_name:
            host = s.execute(select(Host).where(Host.name == host_name)).scalar_one_or_none()
        if host is None:
            return ActionResult(ok=False, summary=f"host not found: {host_id or host_name!r}")

        if host.protocol != "ssh":
            return ActionResult(
                ok=False, summary=f"host {host.name!r} protocol is {host.protocol}, not ssh"
            )

        cred: Credential | None = host.credential
        target_user = (cred.username if cred else None) or "root"
        password: str | None = None
        client_key: str | None = None
        if cred is not None:
            try:
                secret_bytes = get_backend(cfg).get(cred.secret_ref)
            except SecretNotFound:
                return ActionResult(
                    ok=False, summary=f"credential {cred.name!r} secret_ref {cred.secret_ref!r} missing"
                )
            secret_text = secret_bytes.decode("utf-8", errors="replace")
            if cred.kind == "ssh_key":
                client_key = secret_text
            else:
                password = secret_text
        host_target = host.hostname
        host_port = host.port
        host_label = host.name

    started = time.monotonic()
    try:
        connect_kwargs: dict[str, Any] = {
            "host": host_target,
            "port": host_port,
            "username": target_user,
            "known_hosts": None,  # caller is expected to manage trust at the network layer
        }
        if client_key:
            connect_kwargs["client_keys"] = [asyncssh.import_private_key(client_key)]
        if password:
            connect_kwargs["password"] = password
        async with asyncssh.connect(**connect_kwargs) as conn:
            res = await asyncio.wait_for(conn.run(command, check=False), timeout=timeout)
        duration_ms = int((time.monotonic() - started) * 1000)
        ok = res.exit_status == 0
        summary = (
            f"{host_label}: {command} → exit {res.exit_status} in {duration_ms}ms"
            if ok
            else f"{host_label}: {command} FAILED (exit {res.exit_status})"
        )
        return ActionResult(
            ok=ok,
            summary=summary,
            stdout=str(res.stdout or ""),
            stderr=str(res.stderr or ""),
            exit_code=int(res.exit_status) if res.exit_status is not None else None,
            duration_ms=duration_ms,
            extra={"host": host_label, "command": command},
        )
    except asyncio.TimeoutError:
        return ActionResult(
            ok=False,
            summary=f"{host_label}: {command} timed out after {timeout}s",
            duration_ms=int((time.monotonic() - started) * 1000),
            extra={"host": host_label, "command": command},
        )
    except Exception as e:
        return ActionResult(
            ok=False,
            summary=f"{host_label}: {type(e).__name__}: {e}",
            stderr=str(e),
            duration_ms=int((time.monotonic() - started) * 1000),
            extra={"host": host_label, "command": command},
        )


def _ssh_exec_classify(args: dict) -> str:
    # Late-bound config lookup so registration doesn't depend on a Config
    # instance; the runner-side classify happens in routes.py with the live cfg.
    return "safe" if args.get("_pre_classified") == "safe" else "elevated"


SSH_EXEC = ActionSpec(
    name="ssh_exec",
    description="Run a shell command on a host in the inventory over SSH and return stdout/stderr/exit.",
    args_schema=[
        {"name": "host", "type": "string", "required": True, "description": "Host name from the inventory."},
        {"name": "command", "type": "string", "required": True, "description": "Shell command to run."},
        {"name": "timeout", "type": "number", "required": False, "description": "Seconds. Default 30."},
    ],
    runner=_ssh_exec_runner,
    classify=_ssh_exec_classify,
)
register_action(SSH_EXEC)
