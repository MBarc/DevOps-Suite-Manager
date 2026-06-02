"""Pick a file-transfer backend for a host and resolve its effective target.

File transfer is a *capability* configured on the host record (``ft_method`` /
``ft_port`` / ``ft_credential``), separate from the host's primary remote-access
protocol. ``ftps`` is *explicit* FTPS (AUTH TLS over the control port).
"""
from __future__ import annotations

import tempfile
from dataclasses import dataclass

from sqlalchemy.orm import Session

from dosm.config import Config
from dosm.ftp.base import FileTransferBackend, FileTransferError
from dosm.models import Credential, Host

# Stage host-to-host transfers in memory up to this size, then spill to disk.
_STAGE_MAX_MEMORY = 16 * 1024 * 1024

# File-transfer methods a host can expose, with their default ports.
DEFAULT_PORTS = {"sftp": 22, "ftp": 21, "ftps": 21}
FILE_TRANSFER_METHODS = tuple(DEFAULT_PORTS)


def host_has_file_transfer(host: Host) -> bool:
    """True if the host has a file-transfer method configured."""
    return bool(host.ft_method)


@dataclass
class FtTarget:
    """The resolved file-transfer endpoint for a host."""

    method: str            # sftp | ftp | ftps
    port: int
    credential: Credential | None


def resolve_ft_target(host: Host) -> FtTarget:
    """Resolve method, port, and effective credential for ``host``.

    Port falls back to the method default; credential falls back to the host's
    primary credential when no file-transfer override is set.
    """
    method = host.ft_method
    if not method:
        raise FileTransferError(
            f"file transfer is not configured on host {host.name!r}"
        )
    port = host.ft_port or DEFAULT_PORTS.get(method, 0)
    credential = host.ft_credential or host.credential
    return FtTarget(method=method, port=port, credential=credential)


def credential_material(cfg: Config, cred: Credential | None) -> tuple[str, str | None, str | None]:
    """Resolve a credential to (username, password, private_key) from secrets.

    No credential to anonymous. SSH-key creds yield a private key (valid for
    SFTP, rejected by the FTP backend).
    """
    from dosm.secrets import SecretNotFound, get_backend

    if cred is None:
        return "anonymous", None, None
    try:
        secret = get_backend(cfg).get_str(cred.secret_ref)
    except SecretNotFound as e:
        raise FileTransferError(
            f"credential {cred.name!r} secret is missing from the secrets backend"
        ) from e
    if cred.kind == "ssh_key":
        return cred.username or "root", None, secret
    return cred.username or "anonymous", secret, None


def get_file_backend(cfg: Config, db: Session, host: Host) -> FileTransferBackend:
    """Return the backend for ``host`` based on its configured ft_method."""
    target = resolve_ft_target(host)
    if target.method == "sftp":
        from dosm.ftp.sftp_backend import SftpBackend

        return SftpBackend(cfg, db, host, port=target.port, credential=target.credential)
    if target.method in ("ftp", "ftps"):
        from dosm.ftp.ftp_backend import FtpBackend

        return FtpBackend(
            cfg, db, host,
            use_tls=target.method == "ftps",
            port=target.port,
            credential=target.credential,
        )
    raise FileTransferError(
        f"host {host.name!r} has file-transfer method {target.method!r}, which "
        f"is not one of {', '.join(FILE_TRANSFER_METHODS)}"
    )


async def transfer_between_hosts(
    cfg: Config,
    db: Session,
    src_host: Host,
    src_path: str,
    dst_host: Host,
    dst_path: str,
    *,
    move: bool = False,
) -> int:
    """Copy a single file from ``src_host`` to ``dst_host``, server-side.

    Brokered entirely by DOSM: the bytes are staged in a temp file, never
    routed through the operator's browser. Each side goes through its own
    backend via ``get_file_backend`` — so a jumped source and/or a jumped
    destination each traverse their own jump chain transparently; this
    function must never open a connection itself. With ``move=True`` the
    source file is deleted after a successful store. Returns bytes transferred.
    """
    src_backend = get_file_backend(cfg, db, src_host)
    dst_backend = get_file_backend(cfg, db, dst_host)

    spool = tempfile.SpooledTemporaryFile(max_size=_STAGE_MAX_MEMORY)
    try:
        # Retrieve (through the source's jump chain) fully, then store (through
        # the destination's). Sequential, so the two tunnels never contend.
        size = await src_backend.retrieve(src_path, spool)
        spool.seek(0)
        await dst_backend.store(dst_path, spool)
    finally:
        spool.close()

    if move:
        await src_backend.delete(src_path)
    return size
