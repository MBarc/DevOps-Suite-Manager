"""A small, blocking FTP / explicit-FTPS client built for jumped targets.

Why hand-rolled and blocking rather than ``ftplib`` or an async client:

* **TLS session reuse.** Strict FTPS servers (vsftpd's default
  ``require_ssl_reuse=YES``) demand the data connection resume the control
  connection's TLS session. ``ftplib.FTP_TLS`` does *not* do this, and
  asyncio's ``start_tls`` has no way to pass a session. The blocking
  ``ssl`` API does: ``ctx.wrap_socket(sock, session=control.session)``.
  That single capability is the reason this module exists.
* **Pluggable socket factory.** Every control/data socket is opened through
  an injected ``SockFactory`` so the same code path serves a direct host
  (plain ``socket.create_connection``) and a jumped host (SOCKS5 through an
  ``asyncssh`` listener) — see ``dosm/ftp/socks.py``.
* **Passive only.** Active mode requires the server to connect back to the
  client, which is unroutable through a jump. We always use EPSV/PASV and,
  for PASV, ignore the server-advertised IP (often a wrong NAT/internal
  address) in favour of the control host.

Callers run this in a thread executor; see ``dosm/ftp/service.py``.
"""
from __future__ import annotations

import re
import socket
import ssl
from typing import BinaryIO

from dosm.ftp.base import RemoteEntry
from dosm.ftp.socks import SockFactory

_CRLF = b"\r\n"
_DEFAULT_TIMEOUT = 30.0
_BLOCK = 64 * 1024


class FtpError(Exception):
    """An FTP command returned an unexpected (usually 4xx/5xx) reply."""

    def __init__(self, code: int | None, message: str) -> None:
        self.code = code
        super().__init__(f"{code} {message}" if code else message)


class FtpClient:
    def __init__(
        self,
        host: str,
        port: int,
        *,
        sock_factory: SockFactory,
        use_tls: bool = False,
        ssl_context: ssl.SSLContext | None = None,
        server_hostname: str | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
        trust_pasv_ip: bool = False,
    ) -> None:
        self.host = host
        self.port = port
        self._sf = sock_factory
        self.use_tls = use_tls
        self.timeout = timeout
        self.trust_pasv_ip = trust_pasv_ip
        self.server_hostname = server_hostname or host
        self._ssl_context = ssl_context
        if use_tls and self._ssl_context is None:
            # Default to a permissive client context: these are internal
            # hosts reached over an already-encrypted SSH jump, frequently
            # with self-signed certs. Verification is opt-in by passing a
            # configured context.
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            self._ssl_context = ctx
        self._ctrl: socket.socket | None = None
        self._ctrl_reader = None  # buffered file object over the control socket
        self._tls_session: ssl.SSLSession | None = None

    # ── lifecycle ────────────────────────────────────────────────────────
    def connect(self) -> None:
        raw = self._sf(self.host, self.port, self.timeout)
        raw.settimeout(self.timeout)
        self._bind_control(raw)
        self._expect((220,))
        if self.use_tls:
            self._send("AUTH TLS")
            self._expect((234,))
            tls = self._wrap(raw, save_session=True)
            self._bind_control(tls)

    def login(self, username: str, password: str) -> None:
        self._send(f"USER {username}")
        code, _ = self._read_reply()
        if code == 331:
            self._send(f"PASS {password}", secret=True)
            self._expect((230,))
        elif code != 230:
            raise FtpError(code, "login rejected")
        if self.use_tls:
            self._send("PBSZ 0")
            self._read_reply()
            self._send("PROT P")
            self._expect((200,))
        self._send("TYPE I")
        self._expect((200,))

    def quit(self) -> None:
        try:
            if self._ctrl is not None:
                self._send("QUIT")
                self._read_reply()
        except OSError:
            pass
        finally:
            self._close_control()

    def __enter__(self) -> FtpClient:
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.quit()

    # ── directory / metadata commands ────────────────────────────────────
    def pwd(self) -> str:
        self._send("PWD")
        _, text = self._expect((257,))
        m = re.search(r'"((?:[^"]|"")*)"', text)
        return m.group(1).replace('""', '"') if m else text.strip()

    def cwd(self, path: str) -> None:
        self._send(f"CWD {path}")
        self._expect((250,))

    def size(self, path: str) -> int | None:
        self._send(f"SIZE {path}")
        code, text = self._read_reply()
        if code == 213:
            try:
                return int(text.strip())
            except ValueError:
                return None
        return None

    def mkd(self, path: str) -> None:
        self._send(f"MKD {path}")
        self._expect((257,))

    def rmd(self, path: str) -> None:
        self._send(f"RMD {path}")
        self._expect((250,))

    def delete(self, path: str) -> None:
        self._send(f"DELE {path}")
        self._expect((250,))

    def rename(self, src: str, dst: str) -> None:
        self._send(f"RNFR {src}")
        self._expect((350,))
        self._send(f"RNTO {dst}")
        self._expect((250,))

    def list_dir(self, path: str = "") -> list[RemoteEntry]:
        """List ``path`` (default: cwd). Prefers MLSD, falls back to LIST."""
        suffix = f" {path}" if path else ""
        data = self._open_data()
        try:
            self._send(f"MLSD{suffix}")
            code, _ = self._read_reply()
            if code in (125, 150):
                raw = self._drain(data)
                self._expect((226, 250))
                return _parse_mlsd(raw)
        except FtpError:
            pass
        finally:
            _shutdown_data(data)
        # MLSD unsupported — fall back to a fresh LIST data connection.
        data = self._open_data()
        try:
            self._send(f"LIST{suffix}")
            self._expect((125, 150))
            raw = self._drain(data)
        finally:
            _shutdown_data(data)
        self._expect((226, 250))
        return _parse_list(raw)

    # ── transfers ────────────────────────────────────────────────────────
    def retrieve(self, path: str, dest: BinaryIO) -> int:
        """Download ``path`` into the binary file object ``dest``. Returns bytes."""
        data = self._open_data()
        written = 0
        try:
            self._send(f"RETR {path}")
            self._expect((125, 150))
            while True:
                chunk = data.recv(_BLOCK)
                if not chunk:
                    break
                dest.write(chunk)
                written += len(chunk)
        finally:
            _shutdown_data(data)
        self._expect((226, 250))
        return written

    def store(self, path: str, src: BinaryIO) -> int:
        """Upload the binary file object ``src`` to ``path``. Returns bytes sent."""
        data = self._open_data()
        sent = 0
        try:
            self._send(f"STOR {path}")
            self._expect((125, 150))
            while True:
                chunk = src.read(_BLOCK)
                if not chunk:
                    break
                data.sendall(chunk)
                sent += len(chunk)
        finally:
            # A clean TLS shutdown (close_notify) signals EOF; without it a
            # ``tls_data_required`` server treats the upload as aborted and
            # discards it. unwrap() sends close_notify, then the server 226s.
            _shutdown_data(data)
        self._expect((226, 250))
        return sent

    # ── data-connection plumbing ─────────────────────────────────────────
    def _open_data(self) -> socket.socket:
        host, port = self._passive()
        raw = self._sf(host, port, self.timeout)
        raw.settimeout(self.timeout)
        if self.use_tls:
            return self._wrap(raw, save_session=False)
        return raw

    def _passive(self) -> tuple[str, int]:
        # EPSV first: returns a port only, so the data connection always uses
        # the control host — no chance of a bogus PASV IP.
        self._send("EPSV")
        code, text = self._read_reply()
        if code == 229:
            m = re.search(r"\(([!-~])\1\1(\d+)\1\)", text)
            if m:
                return self.host, int(m.group(2))
        # Fall back to PASV.
        self._send("PASV")
        _, text = self._expect((227,))
        nums = re.findall(r"\d+", text)
        if len(nums) < 6:
            raise FtpError(227, f"could not parse PASV reply: {text!r}")
        p1, p2 = int(nums[-2]), int(nums[-1])
        port = p1 * 256 + p2
        if self.trust_pasv_ip:
            return ".".join(nums[0:4]), port
        return self.host, port

    # ── TLS ──────────────────────────────────────────────────────────────
    def _wrap(self, raw: socket.socket, *, save_session: bool) -> ssl.SSLSocket:
        assert self._ssl_context is not None
        kwargs: dict = {"server_hostname": self.server_hostname}
        if not save_session and self._tls_session is not None:
            kwargs["session"] = self._tls_session
        tls = self._ssl_context.wrap_socket(raw, **kwargs)
        if save_session:
            # Capture the negotiated session so PROT P data connections can
            # resume it — the thing ftplib cannot do.
            self._tls_session = tls.session
        return tls

    # ── control-channel I/O ──────────────────────────────────────────────
    def _bind_control(self, sock: socket.socket) -> None:
        self._ctrl = sock
        self._ctrl_reader = sock.makefile("rb")

    def _close_control(self) -> None:
        for closer in (self._ctrl_reader, self._ctrl):
            try:
                if closer is not None:
                    closer.close()
            except OSError:
                pass
        self._ctrl_reader = None
        self._ctrl = None

    def _send(self, command: str, *, secret: bool = False) -> None:
        if self._ctrl is None:
            raise FtpError(None, "control connection is not open")
        self._ctrl.sendall(command.encode("utf-8") + _CRLF)

    def _read_reply(self) -> tuple[int, str]:
        """Read one (possibly multi-line) FTP reply; return (code, text)."""
        assert self._ctrl_reader is not None
        line = self._ctrl_reader.readline()
        if not line:
            raise FtpError(None, "control connection closed by server")
        text = line.decode("utf-8", "replace").rstrip("\r\n")
        if len(text) < 4 or not text[:3].isdigit():
            raise FtpError(None, f"malformed reply: {text!r}")
        code = int(text[:3])
        if text[3] == "-":  # multi-line: read until "NNN <space>"
            terminator = text[:3] + " "
            lines = [text]
            while True:
                more = self._ctrl_reader.readline()
                if not more:
                    break
                mtext = more.decode("utf-8", "replace").rstrip("\r\n")
                lines.append(mtext)
                if mtext.startswith(terminator):
                    break
            text = "\n".join(lines)
        return code, text[4:] if len(text) > 4 else ""

    def _expect(self, codes: tuple[int, ...]) -> tuple[int, str]:
        code, text = self._read_reply()
        if code not in codes:
            raise FtpError(code, text.strip() or f"expected one of {codes}")
        return code, text

    @staticmethod
    def _drain(data: socket.socket) -> bytes:
        chunks: list[bytes] = []
        while True:
            try:
                chunk = data.recv(_BLOCK)
            except ssl.SSLError:
                break
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)


def _shutdown_data(sock: socket.socket) -> None:
    """Close a data socket, performing a clean TLS shutdown when applicable.

    A ``tls_data_required`` server distinguishes a complete transfer from an
    aborted one by the TLS close_notify, so on a PROT P connection we must
    ``unwrap()`` (which sends close_notify and waits for the peer's) before
    closing the underlying TCP socket.
    """
    try:
        if isinstance(sock, ssl.SSLSocket):
            try:
                sock.unwrap()
            except (ssl.SSLError, OSError):
                pass
    finally:
        try:
            sock.close()
        except OSError:
            pass


# ── listing parsers ──────────────────────────────────────────────────────
def _parse_mlsd(raw: bytes) -> list[RemoteEntry]:
    entries: list[RemoteEntry] = []
    for line in raw.decode("utf-8", "replace").splitlines():
        if not line.strip():
            continue
        facts_part, _, name = line.partition(" ")
        if not name:
            continue
        facts = {}
        for fact in facts_part.split(";"):
            if "=" in fact:
                k, v = fact.split("=", 1)
                facts[k.lower()] = v
        typ = facts.get("type", "").lower()
        if typ in ("cdir", "pdir"):  # "." and ".." — skip
            continue
        is_dir = typ == "dir"
        size = None
        if not is_dir and facts.get("size", "").isdigit():
            size = int(facts["size"])
        entries.append(
            RemoteEntry(name=name, is_dir=is_dir, size=size, modify=facts.get("modify"))
        )
    return entries


_LIST_RE = re.compile(
    r"^([\-dl])\S*\s+\d+\s+\S+\s+\S+\s+(\d+)\s+(\w+\s+\d+\s+[\d:]+)\s+(.+)$"
)


def _parse_list(raw: bytes) -> list[RemoteEntry]:
    """Best-effort parse of unix ``ls -l`` style LIST output (fallback)."""
    entries: list[RemoteEntry] = []
    for line in raw.decode("utf-8", "replace").splitlines():
        m = _LIST_RE.match(line.strip())
        if not m:
            continue
        kind, size, modify, name = m.groups()
        if kind == "l":  # symlink: strip "name -> target"
            name = name.split(" -> ", 1)[0]
        if name in (".", ".."):
            continue
        is_dir = kind == "d"
        entries.append(
            RemoteEntry(
                name=name,
                is_dir=is_dir,
                size=None if is_dir else int(size),
                modify=modify,
            )
        )
    return entries
