"""Resolve a host's full jump chain and open a chained asyncssh connection.

The chain order is [outermost_jump, ..., innermost_jump], i.e. the first jump
that DOSM contacts directly. ``connect_through_chain`` opens each in order,
passing the previous as ``tunnel=`` to the next, so the final return value is
an asyncssh connection to the requested target.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from dosm.config import Config
from dosm.models import Credential, Host
from dosm.secrets import SecretNotFound, get_backend


@dataclass
class HopCreds:
    """Resolved per-hop connection material - pure values so it survives
    detachment from the originating SQLAlchemy session."""

    host_id: int
    name: str
    hostname: str
    port: int
    protocol: str   # "ssh" | "rdp" | "vnc" - drives SSH vs WinRM routing
    username: str
    password: str | None
    private_key: str | None


def _resolve_creds(cfg: Config, host: Host) -> HopCreds:
    cred: Credential | None = host.credential
    username = (cred.username if cred and cred.username else None) or "root"
    password: str | None = None
    private_key: str | None = None
    if cred is not None:
        try:
            secret_text = get_backend(cfg).get_str(cred.secret_ref)
        except SecretNotFound as e:
            raise RuntimeError(
                f"credential {cred.name!r} secret_ref {cred.secret_ref!r} missing"
            ) from e
        if cred.kind == "ssh_key":
            private_key = secret_text
        else:
            password = secret_text
    return HopCreds(
        host_id=host.id,
        name=host.name,
        hostname=host.hostname,
        port=host.port,
        protocol=host.protocol,
        username=username,
        password=password,
        private_key=private_key,
    )


def build_jump_chain(db: Session, cfg: Config, host: Host) -> tuple[list[HopCreds], HopCreds]:
    """Return (jump_hops, target). jump_hops is empty if the target is direct."""
    # Imported here, not at module top, to break the dosm.jumps ↔ dosm.hosts
    # import cycle (hosts.routes imports from dosm.jumps). Keeps `import
    # dosm.jumps` working cold, independent of import order.
    from dosm.hosts.repo import resolve_jump_chain

    chain = resolve_jump_chain(db, host)
    jump_hops = [_resolve_creds(cfg, h) for h in chain]
    target = _resolve_creds(cfg, host)
    return jump_hops, target


def _connect_kwargs(hop: HopCreds, *, tunnel=None) -> dict:
    import asyncssh  # type: ignore

    kwargs: dict = {
        "host": hop.hostname,
        "port": hop.port,
        "username": hop.username,
        "known_hosts": None,
    }
    if hop.private_key:
        kwargs["client_keys"] = [asyncssh.import_private_key(hop.private_key)]
    if hop.password:
        kwargs["password"] = hop.password
    if tunnel is not None:
        kwargs["tunnel"] = tunnel
    return kwargs


async def connect_through_chain(jump_hops: list[HopCreds], target: HopCreds):
    """Open each hop in order. Returns the final asyncssh connection.

    Caller is responsible for ``conn.close()`` (and asyncssh will tear down
    the underlying tunnels). For pooled jump connections, use
    JumpTunnelManager instead.
    """
    import asyncssh  # type: ignore

    prev = None
    for hop in jump_hops:
        prev = await asyncssh.connect(**_connect_kwargs(hop, tunnel=prev))
    return await asyncssh.connect(**_connect_kwargs(target, tunnel=prev))
