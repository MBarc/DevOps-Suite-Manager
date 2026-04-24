from __future__ import annotations

import platform
import shutil
from dataclasses import dataclass, field

from dosm.config import CustomTerminal, TerminalsConfig


@dataclass
class Shell:
    """A launchable shell entry shown on the Terminals page."""

    id: str                       # stable key used in URLs
    name: str                     # display label
    command: list[str]            # argv
    source: str                   # "auto" | "custom"
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    description: str | None = None


# (id, display, argv) candidates per platform; checked against PATH.
_POSIX_CANDIDATES: list[tuple[str, str, list[str]]] = [
    ("bash", "bash", ["bash"]),
    ("zsh", "zsh", ["zsh"]),
    ("sh", "sh", ["sh"]),
    ("pwsh", "PowerShell 7 (pwsh)", ["pwsh", "-NoLogo"]),
]

_WINDOWS_CANDIDATES: list[tuple[str, str, list[str]]] = [
    ("pwsh", "PowerShell 7 (pwsh)", ["pwsh.exe", "-NoLogo", "-NoProfile"]),
    ("powershell", "Windows PowerShell", ["powershell.exe", "-NoLogo", "-NoProfile"]),
    ("cmd", "Command Prompt (cmd)", ["cmd.exe"]),
]


def _auto_detect() -> list[Shell]:
    candidates = _WINDOWS_CANDIDATES if platform.system() == "Windows" else _POSIX_CANDIDATES
    found: list[Shell] = []
    for sid, name, argv in candidates:
        exe = shutil.which(argv[0])
        if exe is None:
            continue
        resolved = [exe, *argv[1:]]
        found.append(Shell(id=sid, name=name, command=resolved, source="auto"))
    return found


def _from_custom(entries: list[CustomTerminal]) -> list[Shell]:
    shells: list[Shell] = []
    for i, c in enumerate(entries):
        sid = f"custom-{i}"
        shells.append(
            Shell(
                id=sid,
                name=c.name,
                command=list(c.command),
                source="custom",
                cwd=c.cwd,
                env=dict(c.env),
                description=c.description,
            )
        )
    return shells


def discover_shells(cfg: TerminalsConfig) -> list[Shell]:
    """Return the list of Shell entries to show on the Terminals page.

    Auto-detected shells come first, in platform candidate order, followed by
    any user-defined custom entries. Duplicate ids are disambiguated by
    appending an index suffix.
    """
    shells: list[Shell] = []
    if cfg.auto_detect:
        shells.extend(_auto_detect())
    shells.extend(_from_custom(cfg.custom))

    seen: dict[str, int] = {}
    for s in shells:
        if s.id in seen:
            seen[s.id] += 1
            s.id = f"{s.id}-{seen[s.id]}"
        else:
            seen[s.id] = 0
    return shells


def find_shell(shells: list[Shell], shell_id: str) -> Shell | None:
    for s in shells:
        if s.id == shell_id:
            return s
    # Ephemeral run-as shells live in a separate registry keyed by a token
    # that starts with `ra-`. Importing inside the function avoids a circular
    # import at module load.
    if shell_id.startswith("ra-"):
        from dosm.terminals.runas import get as _runas_get
        return _runas_get(shell_id)
    return None
