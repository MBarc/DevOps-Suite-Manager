"""FTP / explicit-FTPS backend.

Wraps the blocking ``FtpClient`` (dosm/ftp/ftp_client.py): for a jumped host it
leases a SOCKS proxy from ``JumpTunnelManager`` and routes every socket through
it; for a direct host it dials sockets straight. The blocking client runs in a
thread executor so the event loop stays free to service the SOCKS listener —
the concurrency model validated in the Phase A spike.

One FTP control connection is opened per operation (stateless). The *jump*
connection underneath is pooled and shared, so only the lightweight FTP login
+ TLS handshake repeats per call.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import BinaryIO, TypeVar

from sqlalchemy.orm import Session

from dosm.config import Config
from dosm.ftp.base import FileTransferBackend, FileTransferError, RemoteEntry
from dosm.ftp.ftp_client import FtpClient, FtpError
from dosm.ftp.service import credential_material
from dosm.ftp.socks import make_direct_factory, make_socks_factory
from dosm.models import Credential, Host

_T = TypeVar("_T")


def resolve_ftp_login(cfg: Config, cred: Credential | None) -> tuple[str, str]:
    """Resolve (username, password) for FTP from a credential profile.

    No credential to anonymous. An SSH-key credential is rejected: FTP cannot
    use a private key.
    """
    if cred is None:
        return "anonymous", "anonymous@dosm"
    if cred.kind == "ssh_key":
        raise FileTransferError(
            f"credential {cred.name!r} is an SSH key; FTP/FTPS needs a "
            f"username/password credential"
        )
    username, password, _ = credential_material(cfg, cred)
    return username, password or ""


def _run_session(
    host: str,
    port: int,
    username: str,
    password: str,
    use_tls: bool,
    factory,
    op: Callable[[FtpClient], _T],
) -> _T:
    """Open a control connection, log in, run ``op``, always quit. Blocking."""
    client = FtpClient(host, port, sock_factory=factory, use_tls=use_tls)
    try:
        client.connect()
        client.login(username, password)
        return op(client)
    except FtpError as e:
        raise FileTransferError(str(e)) from e
    except OSError as e:
        raise FileTransferError(f"{host}:{port} — {e}") from e
    finally:
        client.quit()


class FtpBackend(FileTransferBackend):
    def __init__(
        self,
        cfg: Config,
        db: Session,
        host: Host,
        *,
        use_tls: bool,
        port: int,
        credential: Credential | None,
    ) -> None:
        self.cfg = cfg
        self.db = db
        self.host = host
        self.use_tls = use_tls
        self.port = port
        self.credential = credential

    async def _run(self, op: Callable[[FtpClient], _T]) -> _T:
        # Lazy import: dosm.jumps ↔ dosm.hosts.routes form an import cycle that
        # only resolves once both packages are initialized (see executor.py).
        from dosm.jumps import get_tunnel_manager

        username, password = resolve_ftp_login(self.cfg, self.credential)
        lease = await get_tunnel_manager().acquire_socks(self.db, self.cfg, self.host)
        factory = (
            make_socks_factory(lease.bind_host, lease.bind_port)
            if lease is not None
            else make_direct_factory()
        )
        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                None,
                _run_session,
                self.host.hostname,
                self.port,
                username,
                password,
                self.use_tls,
                factory,
                op,
            )
        finally:
            if lease is not None:
                await lease.release()

    async def list_dir(self, path: str = "") -> list[RemoteEntry]:
        return await self._run(lambda c: c.list_dir(path))

    async def retrieve(self, path: str, dest: BinaryIO) -> int:
        return await self._run(lambda c: c.retrieve(path, dest))

    async def store(self, path: str, src: BinaryIO) -> int:
        return await self._run(lambda c: c.store(path, src))

    async def delete(self, path: str) -> None:
        await self._run(lambda c: c.delete(path))

    async def mkdir(self, path: str) -> None:
        await self._run(lambda c: c.mkd(path))

    async def rmdir(self, path: str) -> None:
        await self._run(lambda c: c.rmd(path))

    async def rename(self, src: str, dst: str) -> None:
        await self._run(lambda c: c.rename(src, dst))
