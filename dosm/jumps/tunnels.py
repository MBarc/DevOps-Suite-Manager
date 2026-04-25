"""Process-wide pool of jump-host SSH connections + multiplexed local forwards.

Each jump hop is keyed by ``(host_id, jump_chain_signature)`` so an
identical chain reuses the same TCP/auth pair to its outermost jump. Each
forward to a target host is leased; when all leases for a forward are
released, the listener closes. When all forwards on a jump are gone, an
idle timer schedules the jump SSH connection for teardown.

This solves the "concurrent sessions through one jump kicks each other
out" worry: opening N targets behind the same jump uses one auth and N
multiplexed channels, not N reauths.
"""
from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from dosm.config import Config
from dosm.jumps.connections import HopCreds, _connect_kwargs, build_jump_chain
from dosm.models import Host

IDLE_CLOSE_AFTER_SECONDS = 300.0  # tear down a jump SSH conn after 5min idle


def _chain_signature(jump_hops: list[HopCreds]) -> tuple[int, ...]:
    return tuple(h.host_id for h in jump_hops)


@dataclass
class _ForwardEntry:
    listener: Any
    bind_host: str
    bind_port: int
    target_host: str
    target_port: int
    lease_count: int = 0


@dataclass
class _JumpEntry:
    chain_sig: tuple[int, ...]
    conn: Any  # asyncssh.SSHClientConnection
    forwards: dict[tuple[str, int], _ForwardEntry] = field(default_factory=dict)
    last_active: float = field(default_factory=time.monotonic)


@dataclass
class TunnelLease:
    """A leased local forward. Caller must `await release()` when done."""

    bind_host: str
    bind_port: int
    target_host: str
    target_port: int
    _manager: "JumpTunnelManager"
    _key: tuple[tuple[int, ...], str, int]
    _released: bool = False

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        await self._manager._release(self._key)


class JumpTunnelManager:
    """Singleton owning all open jump-host SSH connections."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        # key: chain_sig
        self._jumps: dict[tuple[int, ...], _JumpEntry] = {}

    async def acquire(
        self,
        db: Session,
        cfg: Config,
        host: Host,
        *,
        bind_host: str = "0.0.0.0",
    ) -> TunnelLease | None:
        """Return a TunnelLease if `host` is jumped; ``None`` for direct hosts.

        The caller uses the lease's bind_host/bind_port as the apparent
        endpoint (e.g. for Guacamole's connection blob).
        """
        jump_hops, target = build_jump_chain(db, cfg, host)
        if not jump_hops:
            return None
        sig = _chain_signature(jump_hops)
        async with self._lock:
            entry = self._jumps.get(sig)
            if entry is None:
                conn = await self._open_chain(jump_hops)
                entry = _JumpEntry(chain_sig=sig, conn=conn)
                self._jumps[sig] = entry
            fkey = (target.hostname, target.port)
            forward = entry.forwards.get(fkey)
            if forward is None:
                listener = await entry.conn.forward_local_port(
                    listen_host=bind_host,
                    listen_port=0,  # OS picks a free port
                    dest_host=target.hostname,
                    dest_port=target.port,
                )
                bound = listener.get_port()
                forward = _ForwardEntry(
                    listener=listener,
                    bind_host=bind_host,
                    bind_port=bound,
                    target_host=target.hostname,
                    target_port=target.port,
                )
                entry.forwards[fkey] = forward
            forward.lease_count += 1
            entry.last_active = time.monotonic()
            return TunnelLease(
                bind_host=forward.bind_host,
                bind_port=forward.bind_port,
                target_host=forward.target_host,
                target_port=forward.target_port,
                _manager=self,
                _key=(sig, forward.target_host, forward.target_port),
            )

    async def _open_chain(self, jump_hops: list[HopCreds]):
        import asyncssh  # type: ignore

        prev = None
        for hop in jump_hops:
            prev = await asyncssh.connect(**_connect_kwargs(hop, tunnel=prev))
        return prev

    async def _release(self, key: tuple[tuple[int, ...], str, int]) -> None:
        sig, target_host, target_port = key
        async with self._lock:
            entry = self._jumps.get(sig)
            if entry is None:
                return
            forward = entry.forwards.get((target_host, target_port))
            if forward is None:
                return
            forward.lease_count -= 1
            if forward.lease_count <= 0:
                try:
                    forward.listener.close()
                except Exception:
                    pass
                entry.forwards.pop((target_host, target_port), None)
            entry.last_active = time.monotonic()

    async def gc(self) -> int:
        """Close jump connections that have no forwards and have been idle for
        IDLE_CLOSE_AFTER_SECONDS. Returns the number of jumps reaped."""
        reaped = 0
        now = time.monotonic()
        async with self._lock:
            for sig in list(self._jumps.keys()):
                e = self._jumps[sig]
                if e.forwards:
                    continue
                if now - e.last_active < IDLE_CLOSE_AFTER_SECONDS:
                    continue
                try:
                    e.conn.close()
                except Exception:
                    pass
                self._jumps.pop(sig, None)
                reaped += 1
        return reaped


_manager: JumpTunnelManager | None = None
_manager_lock = threading.Lock()


def get_tunnel_manager() -> JumpTunnelManager:
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = JumpTunnelManager()
        return _manager
