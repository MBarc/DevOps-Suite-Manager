"""Test doubles for the file-transfer suite: a strict FTPS server, an SFTP
server, and an in-process SSH jump - all in-process so the tests need no
Docker and no external hosts.
"""
from __future__ import annotations

import asyncio
import datetime
import threading
from pathlib import Path

import asyncssh
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import TLS_FTPHandler
from pyftpdlib.servers import FTPServer


def make_self_signed(cert_path: Path) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .sign(key, hashes.SHA256())
    )
    cert_path.write_bytes(
        key.private_bytes(serialization.Encoding.PEM,
                          serialization.PrivateFormat.TraditionalOpenSSL,
                          serialization.NoEncryption())
        + cert.public_bytes(serialization.Encoding.PEM)
    )


class FtpsServer:
    """A control- and data-TLS-required FTPS server on a free localhost port.

    Each instance gets its own handler subclass + passive-port range so several
    servers can run at once (e.g. host-to-host transfer tests) without the
    shared-class-attribute clobbering ``TLS_FTPHandler`` is prone to.
    """

    _pasv_base = 60200

    def __init__(self, root: Path, user: str = "ftpuser", password: str = "ftppw"):
        self.root = root
        self.user = user
        self.password = password
        self.port = 0
        self._server: FTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        cert = self.root / "_test_cert.pem"
        make_self_signed(cert)
        authorizer = DummyAuthorizer()
        authorizer.add_user(self.user, self.password, str(self.root), perm="elradfmw")
        base = FtpsServer._pasv_base
        FtpsServer._pasv_base += 40
        handler = type("TLSHandler", (TLS_FTPHandler,), {})  # isolated per server
        handler.certfile = str(cert)
        handler.authorizer = authorizer
        handler.tls_control_required = True
        handler.tls_data_required = True
        handler.passive_ports = list(range(base, base + 40))
        self._server = FTPServer(("127.0.0.1", 0), handler)
        self.port = self._server.address[1]
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"timeout": 0.2}, daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.close_all()
        if self._thread is not None:
            self._thread.join(timeout=5)


class _JumpServer(asyncssh.SSHServer):
    def begin_auth(self, username: str) -> bool:
        return False  # no auth - a throwaway jump box

    def connection_requested(self, dest_host, dest_port, orig_host, orig_port):
        return True  # permit direct-tcpip forwarding (what the SOCKS proxy uses)


async def start_jump():
    """Start an in-process SSH jump server; returns (server, port)."""
    key = asyncssh.generate_private_key("ssh-rsa")
    server = await asyncssh.create_server(_JumpServer, "127.0.0.1", 0,
                                          server_host_keys=[key])
    return server, server.sockets[0].getsockname()[1]


class _SftpAuthServer(asyncssh.SSHServer):
    def password_auth_supported(self) -> bool:
        return True

    def validate_password(self, username: str, password: str) -> bool:
        return username == "sftpuser" and password == "sftppw"


async def start_sftp_server(root: Path):
    """Start an in-process SFTP server rooted at ``root``; returns (server, port)."""
    key = asyncssh.generate_private_key("ssh-rsa")
    server = await asyncssh.create_server(
        _SftpAuthServer, "127.0.0.1", 0,
        server_host_keys=[key],
        sftp_factory=lambda chan: asyncssh.SFTPServer(chan, chroot=str(root)),
    )
    return server, server.sockets[0].getsockname()[1]


class ThreadedSftpServer:
    """An SFTP server running in its own thread + event loop.

    Lets a synchronous TestClient drive a route whose handler (in the app's
    loop) connects to this server over TCP - without sharing an event loop
    (which would deadlock) or a second in-process TLS stack.
    """

    def __init__(self, root: Path):
        self.root = root
        self.port = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    def start(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._ready.wait(15):
            raise RuntimeError("threaded SFTP server failed to start")

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)

        async def boot():
            self._server, self.port = await start_sftp_server(self.root)
            self._ready.set()

        self._loop.run_until_complete(boot())
        self._loop.run_forever()

    def stop(self) -> None:
        if self._loop is not None and self._server is not None:
            async def _shutdown():
                self._server.close()
                await self._server.wait_closed()
            try:
                asyncio.run_coroutine_threadsafe(_shutdown(), self._loop).result(timeout=5)
            except Exception:
                pass
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5)
