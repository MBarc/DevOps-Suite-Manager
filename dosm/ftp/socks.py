"""Tiny blocking SOCKS5 client used to route FTP/FTPS sockets through a jump.

DOSM never talks FTP to a jumped target directly. Instead the jump-tunnel
layer opens an ``asyncssh`` SOCKS5 listener (``conn.forward_socks``) on a
local loopback port, and every control/data socket the FTP client opens is
dialed through that listener. The jump host does the actual TCP connect to
the FTP server (and to each ephemeral passive-mode data port), so the
"PASV hands back a port that was never forwarded" problem disappears: the
SOCKS proxy forwards *whatever* host:port the connection asks for, on demand.

Hand-rolled (no PySocks dep) and no-auth only — the listener is bound to
loopback inside the DOSM process, so there is nothing to authenticate.
"""
from __future__ import annotations

import socket
from collections.abc import Callable

# A socket factory: given (host, port, timeout) return a connected raw socket.
SockFactory = Callable[[str, int, float], socket.socket]


class SocksError(Exception):
    """SOCKS5 negotiation with the local jump proxy failed."""


def _recv_exactly(sock: socket.socket, n: int) -> bytes:
    chunks: list[bytes] = []
    remaining = n
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise SocksError("SOCKS proxy closed the connection mid-handshake")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


# SOCKS5 reply codes (RFC 1928 §6) to human-readable, so a jump that can't
# reach the FTP target produces a message naming why rather than a bare byte.
_REPLY_MESSAGES = {
    0x01: "general SOCKS server failure",
    0x02: "connection not allowed by ruleset",
    0x03: "network unreachable",
    0x04: "host unreachable",
    0x05: "connection refused by destination",
    0x06: "TTL expired",
    0x07: "command not supported",
    0x08: "address type not supported",
}


def socks5_connect(
    proxy_host: str,
    proxy_port: int,
    dest_host: str,
    dest_port: int,
    timeout: float,
) -> socket.socket:
    """Open a TCP connection to ``dest_host:dest_port`` via a SOCKS5 proxy.

    The destination is sent as a DOMAINNAME address so the *jump* resolves it
    — DOSM may not be able to resolve the target's internal name itself.
    """
    sock = socket.create_connection((proxy_host, proxy_port), timeout=timeout)
    try:
        sock.settimeout(timeout)
        # Greeting: version 5, one method, "no authentication required".
        sock.sendall(b"\x05\x01\x00")
        greeting = _recv_exactly(sock, 2)
        if greeting[0:1] != b"\x05":
            raise SocksError(f"proxy is not SOCKS5 (got version byte {greeting[0]})")
        if greeting[1] != 0x00:
            raise SocksError("proxy demanded authentication; only no-auth is supported")

        host_bytes = dest_host.encode("idna") if _is_hostname(dest_host) else dest_host.encode()
        if len(host_bytes) > 255:
            raise SocksError(f"destination host name too long: {dest_host!r}")
        request = (
            b"\x05\x01\x00\x03"          # ver, CONNECT, reserved, ATYP=domainname
            + bytes([len(host_bytes)])
            + host_bytes
            + dest_port.to_bytes(2, "big")
        )
        sock.sendall(request)

        reply = _recv_exactly(sock, 4)
        if reply[1] != 0x00:
            msg = _REPLY_MESSAGES.get(reply[1], f"SOCKS error 0x{reply[1]:02x}")
            raise SocksError(f"jump could not reach {dest_host}:{dest_port} — {msg}")

        # Drain the bound-address field so the socket is positioned at the
        # start of the tunnelled stream.
        atyp = reply[3]
        if atyp == 0x01:      # IPv4
            _recv_exactly(sock, 4)
        elif atyp == 0x03:    # domain name
            length = _recv_exactly(sock, 1)[0]
            _recv_exactly(sock, length)
        elif atyp == 0x04:    # IPv6
            _recv_exactly(sock, 16)
        else:
            raise SocksError(f"proxy returned unknown address type {atyp}")
        _recv_exactly(sock, 2)  # bound port
        return sock
    except Exception:
        sock.close()
        raise


def _is_hostname(addr: str) -> bool:
    """True if ``addr`` is a name rather than a literal IP (so we IDNA-encode)."""
    try:
        socket.inet_aton(addr)
        return False
    except OSError:
        return ":" not in addr  # crude: treat IPv6 literals as non-hostnames


def make_direct_factory() -> SockFactory:
    """Socket factory for a directly reachable (non-jumped) FTP host."""

    def factory(host: str, port: int, timeout: float) -> socket.socket:
        return socket.create_connection((host, port), timeout=timeout)

    return factory


def make_socks_factory(proxy_host: str, proxy_port: int) -> SockFactory:
    """Socket factory that routes every connection through a SOCKS5 jump proxy."""

    def factory(host: str, port: int, timeout: float) -> socket.socket:
        return socks5_connect(proxy_host, proxy_port, host, port, timeout)

    return factory
