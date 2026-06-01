"""Backend-agnostic file-transfer contract.

A ``FileTransferBackend`` exposes a small, uniform set of operations against a
single inventory ``Host``. Concrete backends:

* ``FtpBackend``  — plain FTP / explicit FTPS via the hand-rolled blocking
  client, routed through an SSH-jump SOCKS proxy when the host is jumped.
* ``SftpBackend`` — SFTP over ``asyncssh``, which tunnels through the jump
  chain natively.

The web file browser, the CLI, and audit logging are written once against
this ABC; picking a backend is ``service.get_backend(...)``'s job. Methods are
async; blocking backends offload to a thread executor internally.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import BinaryIO


@dataclass
class RemoteEntry:
    """One directory entry. Shared by every backend so the UI is uniform."""

    name: str
    is_dir: bool
    size: int | None = None
    modify: str | None = None  # raw server timestamp; rendering is the UI's job


class FileTransferError(Exception):
    """A file-transfer operation failed with an operator-facing message."""


class FileTransferBackend(ABC):
    """Uniform file operations against one host. All paths are POSIX-style."""

    @abstractmethod
    async def list_dir(self, path: str = "") -> list[RemoteEntry]: ...

    @abstractmethod
    async def retrieve(self, path: str, dest: BinaryIO) -> int: ...

    @abstractmethod
    async def store(self, path: str, src: BinaryIO) -> int: ...

    @abstractmethod
    async def delete(self, path: str) -> None: ...

    @abstractmethod
    async def mkdir(self, path: str) -> None: ...

    @abstractmethod
    async def rmdir(self, path: str) -> None: ...

    @abstractmethod
    async def rename(self, src: str, dst: str) -> None: ...
