"""SFTP backend over asyncssh.

When the target runs SSH, SFTP is the simplest and most secure file-transfer
option: asyncssh carries it through the jump chain natively (no SOCKS proxy,
no passive-port juggling). Reuses ``connect_through_chain`` — the documented
one-shot connector — opening a fresh chained connection per operation.
"""
from __future__ import annotations

import stat as statmod
from typing import BinaryIO

from sqlalchemy.orm import Session

from dosm.config import Config
from dosm.ftp.base import FileTransferBackend, FileTransferError, RemoteEntry
from dosm.ftp.service import credential_material
from dosm.models import Credential, Host

_BLOCK = 64 * 1024


class SftpBackend(FileTransferBackend):
    def __init__(
        self,
        cfg: Config,
        db: Session,
        host: Host,
        *,
        port: int,
        credential: Credential | None,
    ) -> None:
        self.cfg = cfg
        self.db = db
        self.host = host
        self.port = port
        self.credential = credential

    async def _connect(self):
        # Lazy import to avoid the dosm.jumps ↔ dosm.hosts import cycle.
        from dosm.jumps import build_jump_chain, connect_through_chain

        jump_hops, target = build_jump_chain(self.db, self.cfg, self.host)
        # Override the target endpoint with the file-transfer port + credential
        # (the jump hops keep their own SSH creds). Jumps are unaffected.
        target.port = self.port
        target.username, target.password, target.private_key = credential_material(
            self.cfg, self.credential
        )
        try:
            return await connect_through_chain(jump_hops, target)
        except Exception as e:  # noqa: BLE001 — surface as an operator message
            raise FileTransferError(
                f"could not open SSH/SFTP to {self.host.name!r} "
                f"({self.host.hostname}:{self.port}): {e}"
            ) from e

    async def list_dir(self, path: str = "") -> list[RemoteEntry]:
        conn = await self._connect()
        try:
            async with conn.start_sftp_client() as sftp:
                names = await sftp.readdir(path or ".")
                entries: list[RemoteEntry] = []
                for n in names:
                    if n.filename in (".", ".."):
                        continue
                    perms = n.attrs.permissions or 0
                    is_dir = statmod.S_ISDIR(perms)
                    entries.append(
                        RemoteEntry(
                            name=n.filename,
                            is_dir=is_dir,
                            size=None if is_dir else n.attrs.size,
                            modify=str(n.attrs.mtime) if n.attrs.mtime else None,
                        )
                    )
                return entries
        finally:
            conn.close()

    async def retrieve(self, path: str, dest: BinaryIO) -> int:
        conn = await self._connect()
        written = 0
        try:
            async with conn.start_sftp_client() as sftp:
                async with sftp.open(path, "rb") as f:
                    while True:
                        chunk = await f.read(_BLOCK)
                        if not chunk:
                            break
                        dest.write(chunk)
                        written += len(chunk)
        finally:
            conn.close()
        return written

    async def store(self, path: str, src: BinaryIO) -> int:
        conn = await self._connect()
        sent = 0
        try:
            async with conn.start_sftp_client() as sftp:
                async with sftp.open(path, "wb") as f:
                    while True:
                        chunk = src.read(_BLOCK)
                        if not chunk:
                            break
                        await f.write(chunk)
                        sent += len(chunk)
        finally:
            conn.close()
        return sent

    async def delete(self, path: str) -> None:
        conn = await self._connect()
        try:
            async with conn.start_sftp_client() as sftp:
                await sftp.remove(path)
        finally:
            conn.close()

    async def mkdir(self, path: str) -> None:
        conn = await self._connect()
        try:
            async with conn.start_sftp_client() as sftp:
                await sftp.mkdir(path)
        finally:
            conn.close()

    async def rmdir(self, path: str) -> None:
        conn = await self._connect()
        try:
            async with conn.start_sftp_client() as sftp:
                await sftp.rmdir(path)
        finally:
            conn.close()

    async def rename(self, src: str, dst: str) -> None:
        conn = await self._connect()
        try:
            async with conn.start_sftp_client() as sftp:
                await sftp.rename(src, dst)
        finally:
            conn.close()
