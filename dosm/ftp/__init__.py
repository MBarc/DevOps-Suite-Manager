"""File transfer (FTP / explicit FTPS / SFTP) for jumped and direct hosts.

* ``FileTransferBackend`` (base) - the uniform contract the UI/CLI use.
* ``FtpBackend`` / ``SftpBackend`` - concrete backends.
* ``get_file_backend`` - pick one for a host by protocol.

The hard problem (FTP's separate, dynamically-negotiated data connection
through a jump box) is solved by routing every FTP socket through an SSH-jump
SOCKS proxy; see ``dosm/ftp/ftp_client.py`` and ``dosm/ftp/socks.py``.
"""
from dosm.ftp.base import (
    FileTransferBackend,
    FileTransferError,
    RemoteEntry,
)
from dosm.ftp.routes import router as ftp_router
from dosm.ftp.service import (
    DEFAULT_PORTS,
    FILE_TRANSFER_METHODS,
    get_file_backend,
    host_has_file_transfer,
    resolve_ft_target,
)

__all__ = [
    "FileTransferBackend",
    "FileTransferError",
    "RemoteEntry",
    "DEFAULT_PORTS",
    "FILE_TRANSFER_METHODS",
    "get_file_backend",
    "host_has_file_transfer",
    "resolve_ft_target",
    "ftp_router",
]
