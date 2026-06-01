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
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from dosm.config import Config
from dosm.jumps.connections import HopCreds, _connect_kwargs, build_jump_chain
from dosm.models import Host

IDLE_CLOSE_AFTER_SECONDS = 300.0  # tear down a jump SSH conn after 5min idle
GC_INTERVAL_SECONDS = 60.0
PROBE_TIMEOUT_SECONDS = 5.0


class JumpUnreachableError(Exception):
    """Could not open a network connection to a jump host."""


class JumpAuthError(Exception):
    """Connected to a jump host but authentication was rejected."""


class TargetUnreachableError(Exception):
    """Jump host cannot forward connections to the target."""


class TargetAuthError(Exception):
    """Reached the target but credentials were rejected."""


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
    # A single SOCKS5 listener multiplexed across all FTP sessions on this
    # jump. Unlike a port forward (one fixed target), a SOCKS proxy reaches
    # *any* host:port on demand — exactly what FTP passive data ports need.
    socks_listener: Any = None
    socks_bind_host: str = ""
    socks_bind_port: int = 0
    socks_lease_count: int = 0


@dataclass
class TunnelLease:
    """A leased local forward. Caller must `await release()` when done."""

    bind_host: str
    bind_port: int
    target_host: str
    target_port: int
    _manager: JumpTunnelManager
    _key: tuple[tuple[int, ...], str, int]
    _released: bool = False

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        await self._manager._release(self._key)


@dataclass
class SocksLease:
    """A leased SOCKS5 proxy through a jump chain. ``await release()`` when done."""

    bind_host: str
    bind_port: int
    _manager: JumpTunnelManager
    _sig: tuple[int, ...]
    _released: bool = False

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        await self._manager._release_socks(self._sig)


class JumpTunnelManager:
    """Singleton owning all open jump-host SSH connections."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        # key: chain_sig
        self._jumps: dict[tuple[int, ...], _JumpEntry] = {}
        # Browser-session registry: maps an opaque session id to the lease
        # held for that connect call. Released on explicit disconnect (e.g.
        # tab close → pagehide beacon) or by the TTL backstop task.
        self._sessions: dict[str, TunnelLease] = {}

    async def acquire(
        self,
        db: Session,
        cfg: Config,
        host: Host,
        *,
        bind_host: str = "0.0.0.0",
        target_port: int | None = None,
    ) -> TunnelLease | None:
        """Return a TunnelLease if `host` is jumped; ``None`` for direct hosts.

        The caller uses the lease's bind_host/bind_port as the apparent
        endpoint (e.g. for Guacamole's connection blob).

        ``target_port`` overrides ``host.port`` for the tunnel destination —
        used by winrm_exec to forward to WinRM (5985/5986) instead of the
        host's registered RDP port (3389).
        """
        jump_hops, target = build_jump_chain(db, cfg, host)
        if not jump_hops:
            return None
        sig = _chain_signature(jump_hops)
        eff_port = target_port if target_port is not None else target.port
        async with self._lock:
            entry = self._jumps.get(sig)
            if entry is None:
                conn = await self._open_chain(jump_hops)
                entry = _JumpEntry(chain_sig=sig, conn=conn)
                self._jumps[sig] = entry
            fkey = (target.hostname, eff_port)
            forward = entry.forwards.get(fkey)
            if forward is None:
                listener = await entry.conn.forward_local_port(
                    listen_host=bind_host,
                    listen_port=0,  # OS picks a free port
                    dest_host=target.hostname,
                    dest_port=eff_port,
                )
                bound = listener.get_port()
                forward = _ForwardEntry(
                    listener=listener,
                    bind_host=bind_host,
                    bind_port=bound,
                    target_host=target.hostname,
                    target_port=eff_port,
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

    async def acquire_socks(
        self,
        db: Session,
        cfg: Config,
        host: Host,
        *,
        bind_host: str = "127.0.0.1",
    ) -> SocksLease | None:
        """Return a SOCKS5 ``SocksLease`` for reaching ``host`` through its jump
        chain; ``None`` for a directly reachable host.

        One SOCKS listener is opened per jump chain and shared by every leaser
        — an FTP control connection plus all of its ephemeral passive data
        ports tunnel through the same proxy, so dynamic data ports need no
        per-transfer forward bookkeeping. Bound to loopback by default: the
        proxy is no-auth and must never be exposed off-box.
        """
        jump_hops, _ = build_jump_chain(db, cfg, host)
        if not jump_hops:
            return None
        sig = _chain_signature(jump_hops)
        async with self._lock:
            entry = self._jumps.get(sig)
            if entry is None:
                conn = await self._open_chain(jump_hops)
                entry = _JumpEntry(chain_sig=sig, conn=conn)
                self._jumps[sig] = entry
            if entry.socks_listener is None:
                listener = await entry.conn.forward_socks(bind_host, 0)
                entry.socks_listener = listener
                entry.socks_bind_host = bind_host
                entry.socks_bind_port = listener.get_port()
            entry.socks_lease_count += 1
            entry.last_active = time.monotonic()
            return SocksLease(
                bind_host=entry.socks_bind_host,
                bind_port=entry.socks_bind_port,
                _manager=self,
                _sig=sig,
            )

    async def _release_socks(self, sig: tuple[int, ...]) -> None:
        async with self._lock:
            entry = self._jumps.get(sig)
            if entry is None:
                return
            entry.socks_lease_count -= 1
            if entry.socks_lease_count <= 0:
                entry.socks_lease_count = 0
                if entry.socks_listener is not None:
                    try:
                        entry.socks_listener.close()
                    except Exception:
                        pass
                    entry.socks_listener = None
                    entry.socks_bind_port = 0
            entry.last_active = time.monotonic()

    async def _open_chain(self, jump_hops: list[HopCreds]):
        import asyncssh  # type: ignore

        prev = None
        for hop in jump_hops:
            try:
                prev = await asyncssh.connect(**_connect_kwargs(hop, tunnel=prev))
            except asyncssh.PermissionDenied as e:
                raise JumpAuthError(
                    f"authentication failed at jump host {hop.name!r} "
                    f"(user {hop.username!r}) — check the credential profile"
                ) from e
            except Exception as e:
                raise JumpUnreachableError(
                    f"cannot connect to jump host {hop.name!r} "
                    f"({hop.hostname}:{hop.port}): {e}"
                ) from e
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

    async def register_session(self, lease: TunnelLease, ttl_seconds: int) -> str:
        """Track ``lease`` under a fresh session id; schedule a TTL backstop.

        The browser holds the id and pings ``release_session`` on tab close
        (pagehide beacon). The backstop guarantees release if the browser
        signal never arrives — kill, network drop, mobile Safari quirk.
        """
        sid = secrets.token_urlsafe(16)
        async with self._lock:
            self._sessions[sid] = lease
        asyncio.create_task(self._auto_release(sid, ttl_seconds))
        return sid

    async def release_session(self, sid: str) -> bool:
        """Release the lease registered under ``sid``. Idempotent."""
        async with self._lock:
            lease = self._sessions.pop(sid, None)
        if lease is None:
            return False
        try:
            await lease.release()
        except Exception:
            pass
        return True

    async def _auto_release(self, sid: str, ttl_seconds: int) -> None:
        try:
            await asyncio.sleep(ttl_seconds)
        except asyncio.CancelledError:
            return
        await self.release_session(sid)

    def stats(self) -> dict:
        """Return a snapshot of pool size for health/diagnostics views.

        Reads private state directly — synchronous and approximate; a
        concurrent acquire/release may shift the numbers by one. That's
        fine for a status display.
        """
        jumps = list(self._jumps.values())
        return {
            "open_jump_connections": len(jumps),
            "open_forwards": sum(len(e.forwards) for e in jumps),
            "open_socks_proxies": sum(1 for e in jumps if e.socks_listener is not None),
            "active_sessions": len(self._sessions),
        }

    async def gc(self) -> int:
        """Close jump connections that have no forwards and have been idle for
        IDLE_CLOSE_AFTER_SECONDS. Returns the number of jumps reaped."""
        reaped = 0
        now = time.monotonic()
        async with self._lock:
            for sig in list(self._jumps.keys()):
                e = self._jumps[sig]
                if e.forwards or e.socks_listener is not None:
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


async def probe_forward(
    lease: TunnelLease, *, timeout: float = PROBE_TIMEOUT_SECONDS
) -> None:
    """Open a TCP connection through the tunnel to verify the target is reachable.

    Connects to the local listener that asyncssh is forwarding to the target.
    If the target refuses or is silent, raises ``TargetUnreachableError`` with
    a human-readable message before Guacamole ever attempts its session.
    """
    # Listener is on bind_host (often 0.0.0.0); probe via loopback.
    probe_addr = (
        "127.0.0.1"
        if lease.bind_host in ("0.0.0.0", "")
        else lease.bind_host
    )
    writer = None
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(probe_addr, lease.bind_port),
            timeout=timeout,
        )
    except TimeoutError:
        raise TargetUnreachableError(
            f"jump box cannot reach {lease.target_host}:{lease.target_port} "
            f"— connection timed out (host unreachable or port filtered)"
        )
    except OSError as e:
        raise TargetUnreachableError(
            f"jump box cannot reach {lease.target_host}:{lease.target_port} "
            f"— {e}"
        )
    finally:
        if writer is not None:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass


async def verify_ssh_credentials(
    *,
    bind_port: int,
    bind_host: str = "0.0.0.0",
    username: str | None,
    password: str | None,
    private_key: str | None,
    target_host: str,
    target_port: int,
    timeout: float = 10.0,
) -> None:
    """Attempt SSH auth to the target through an established tunnel.

    Skipped when no auth material is present (guacd will prompt the user).
    Raises ``TargetAuthError`` if the server rejects the credentials.
    Raises ``TargetUnreachableError`` if the handshake fails for other reasons.
    """
    if not password and not private_key:
        return

    import asyncssh  # type: ignore

    connect_host = "127.0.0.1" if bind_host in ("0.0.0.0", "") else bind_host
    kwargs: dict = {
        "host": connect_host,
        "port": bind_port,
        "username": username or "root",
        "known_hosts": None,
    }
    if private_key:
        kwargs["client_keys"] = [asyncssh.import_private_key(private_key)]
    elif password:
        kwargs["password"] = password

    conn = None
    try:
        conn = await asyncio.wait_for(asyncssh.connect(**kwargs), timeout=timeout)
    except asyncssh.PermissionDenied as e:
        raise TargetAuthError(
            f"target {target_host}:{target_port} rejected credentials "
            f"for user {username!r} — check the credential profile"
        ) from e
    except TimeoutError:
        raise TargetUnreachableError(
            f"target {target_host}:{target_port} timed out during SSH handshake"
        )
    except (TargetUnreachableError, TargetAuthError):
        raise
    except Exception as e:
        raise TargetUnreachableError(
            f"SSH handshake with target {target_host}:{target_port} failed: {e}"
        ) from e
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


async def gc_loop(interval: float = GC_INTERVAL_SECONDS) -> None:
    """Periodically reap idle jump connections. Started from app startup."""
    manager = get_tunnel_manager()
    while True:
        try:
            await asyncio.sleep(interval)
            await manager.gc()
        except asyncio.CancelledError:
            return
        except Exception:
            # Reaper must never die from a transient error — keep ticking.
            continue
