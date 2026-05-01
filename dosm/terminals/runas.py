from __future__ import annotations

import platform
import secrets
import threading
import time
from dataclasses import dataclass

from dosm.terminals.discover import Shell

# Ephemeral shells live in a process-wide registry keyed by an opaque token.
# They expire after EPHEMERAL_TTL seconds whether or not they were opened.
EPHEMERAL_TTL = 300.0


@dataclass
class _Entry:
    shell: Shell
    expires_at: float
    consumed: bool = False


_lock = threading.Lock()
_registry: dict[str, _Entry] = {}


def _gc(now: float | None = None) -> None:
    now = now or time.monotonic()
    stale = [k for k, e in _registry.items() if e.expires_at < now]
    for k in stale:
        _registry.pop(k, None)


def register(shell: Shell, *, ttl: float = EPHEMERAL_TTL) -> str:
    """Register an ephemeral shell. Returns a token that can be used as
    ``shell_id`` on the existing terminal routes."""
    token = "ra-" + secrets.token_urlsafe(12)
    shell.id = token
    with _lock:
        _gc()
        _registry[token] = _Entry(shell=shell, expires_at=time.monotonic() + ttl)
    return token


def get(token: str) -> Shell | None:
    with _lock:
        _gc()
        entry = _registry.get(token)
        return entry.shell if entry else None


def consume(token: str) -> Shell | None:
    """Used when a session attaches; allows future hardening (e.g. one-shot
    tokens) without changing call sites. For now leaves the entry in place
    until TTL expiry so reconnects work."""
    return get(token)


def build_runas_argv(target_user: str, base_argv: list[str]) -> list[str]:
    """Wrap ``base_argv`` so it executes as ``target_user``.

    POSIX -> sudo -u <user> -i <argv>
    Windows -> runas /user:<user> "<argv joined>"

    Both rely on the platform's native credential handling (sudoers /
    runas dialog). Password pass-through via the secrets backend is a
    follow-up — see roadmap.
    """
    if platform.system() == "Windows":
        joined = " ".join(base_argv)
        return ["runas.exe", f"/user:{target_user}", joined]
    return ["sudo", "-u", target_user, "-i", "--", *base_argv]


def make_runas_shell(base: Shell, target_user: str) -> Shell:
    name = f"{base.name} as {target_user}"
    argv = build_runas_argv(target_user, base.command)
    return Shell(
        id="pending",  # replaced by `register`
        name=name,
        command=argv,
        source="runas",
        cwd=base.cwd,
        env=dict(base.env),
        description=f"Wrapped: {' '.join(argv)}",
    )