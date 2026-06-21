"""File-transfer tests: client parsers (unit) + FTPS/SFTP backends and the web
file-browser routes (integration), including the FTPS-through-an-SSH-jump path.

The servers are in-process (pyftpdlib for FTPS, asyncssh for SFTP and the SSH
jump) so the suite needs no Docker and no external hosts.
"""
from __future__ import annotations

import asyncio
import io

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from dosm.auth.passwords import hash_password
from dosm.ftp.ftp_client import _parse_list, _parse_mlsd
from dosm.ftp.service import get_file_backend
from dosm.models import AuditLog, Credential, Host, User
from dosm.secrets import get_backend
from tests._ftp_helpers import (
    FtpsServer,
    ThreadedSftpServer,
    start_jump,
    start_sftp_server,
)


# ── unit: listing parsers ────────────────────────────────────────────────────
def test_parse_mlsd_files_and_dirs():
    raw = (
        b"type=dir;modify=20260101120000; logs\r\n"
        b"type=file;size=42;modify=20260102130000; app.cfg\r\n"
    )
    entries = {e.name: e for e in _parse_mlsd(raw)}
    assert entries["logs"].is_dir and entries["logs"].size is None
    assert not entries["app.cfg"].is_dir
    assert entries["app.cfg"].size == 42
    assert entries["app.cfg"].modify == "20260102130000"


def test_parse_mlsd_skips_dot_entries():
    raw = b"type=cdir; .\r\ntype=pdir; ..\r\ntype=file;size=1; real\r\n"
    names = [e.name for e in _parse_mlsd(raw)]
    assert names == ["real"]


def test_parse_list_unix():
    raw = (
        b"drwxr-xr-x 2 root root 4096 Jan  1 12:00 logs\n"
        b"-rw-r--r-- 1 root root  128 Jan  2 13:00 app.cfg\n"
    )
    entries = {e.name: e for e in _parse_list(raw)}
    assert entries["logs"].is_dir
    assert entries["app.cfg"].size == 128


# ── fixtures ─────────────────────────────────────────────────────────────────
@pytest.fixture
def ftps(tmp_path):
    root = tmp_path / "ftproot"
    root.mkdir()
    (root / "readme.txt").write_bytes(b"hello from ftps\n")
    (root / "sub").mkdir()
    srv = FtpsServer(root)
    srv.start()
    yield srv, root
    srv.stop()


def _make_host(session_factory, test_config, *, port, method, username, password,
               name, jump_host_id=None):
    """Create an ssh host with file transfer configured via ft_* fields."""
    from sqlalchemy import text

    with session_factory() as s:
        tid = s.execute(text("SELECT id FROM tenants WHERE slug='default'")).scalar_one()
        ref = f"ftp/{name}"
        get_backend(test_config).set_str(ref, password)
        cred = Credential(name=f"cred-{name}", kind="login", username=username,
                          secret_ref=ref, tenant_id=tid)
        s.add(cred)
        s.flush()
        host = Host(name=name, hostname="127.0.0.1", port=22, protocol="ssh",
                    credential_id=cred.id, jump_host_id=jump_host_id,
                    ft_method=method, ft_port=port, tenant_id=tid)
        s.add(host)
        s.commit()
        return host.id


# ── routes: direct FTPS host ─────────────────────────────────────────────────
def test_browser_page_renders(auth_client, session_factory, test_config, ftps):
    srv, _ = ftps
    hid = _make_host(session_factory, test_config, port=srv.port, method="ftps",
                     username="ftpuser", password="ftppw", name="ftps-page")
    r = auth_client.get(f"/files/{hid}")
    assert r.status_code == 200
    assert "file browser" in r.text.lower()


def test_full_lifecycle_via_routes(auth_client, session_factory, test_config, ftps):
    srv, root = ftps
    hid = _make_host(session_factory, test_config, port=srv.port, method="ftps",
                     username="ftpuser", password="ftppw", name="ftps-life")

    listing = auth_client.get(f"/files/{hid}/list").json()
    names = [e["name"] for e in listing["entries"]]
    assert "readme.txt" in names and "sub" in names

    up = auth_client.post(f"/files/{hid}/upload", data={"path": ""},
                          files={"file": ("data.bin", b"Z" * 256, "application/octet-stream")})
    assert up.json()["bytes"] == 256
    assert (root / "data.bin").read_bytes() == b"Z" * 256

    dl = auth_client.get(f"/files/{hid}/download", params={"path": "readme.txt"})
    assert dl.content == b"hello from ftps\n"

    auth_client.post(f"/files/{hid}/mkdir", data={"path": "", "name": "made"})
    assert (root / "made").is_dir()
    auth_client.post(f"/files/{hid}/rename", data={"path": "", "src": "data.bin", "dst": "renamed.bin"})
    assert (root / "renamed.bin").exists()
    auth_client.post(f"/files/{hid}/delete", data={"path": "renamed.bin"})
    assert not (root / "renamed.bin").exists()
    auth_client.post(f"/files/{hid}/delete", data={"path": "made", "is_dir": "1"})
    assert not (root / "made").exists()

    with session_factory() as s:
        actions = {a.action for a in s.execute(select(AuditLog)).scalars()}
    assert {"host.files.upload", "host.files.download", "host.files.mkdir",
            "host.files.rename", "host.files.delete"} <= actions


def test_non_admin_is_forbidden(app, session_factory, test_config, ftps):
    srv, _ = ftps
    hid = _make_host(session_factory, test_config, port=srv.port, method="ftps",
                     username="ftpuser", password="ftppw", name="ftps-gate")
    with session_factory() as s:
        if not s.execute(select(User).where(User.username == "ftp-operator")).scalar_one_or_none():
            from sqlalchemy import text
            tid = s.execute(text("SELECT id FROM tenants WHERE slug='default'")).scalar_one()
            s.add(User(username="ftp-operator", password_hash=hash_password("pw"),
                       role="operator", tenant_id=tid, is_active=True))
            s.commit()
    c = TestClient(app)
    c.post("/login", data={"username": "ftp-operator", "password": "pw", "next": "/"},
           follow_redirects=False)
    assert c.get(f"/files/{hid}/list").status_code == 403


def test_bad_credential_surfaces_error(auth_client, session_factory, test_config, ftps):
    srv, _ = ftps
    hid = _make_host(session_factory, test_config, port=srv.port, method="ftps",
                     username="ftpuser", password="wrong-password", name="ftps-bad")
    r = auth_client.get(f"/files/{hid}/list")
    assert r.status_code == 502
    assert "error" in r.json()


def test_host_form_sets_and_clears_file_transfer(auth_client, session_factory):
    # Create with file transfer configured.
    auth_client.post("/hosts/new", data={
        "name": "ft-form", "hostname": "10.0.0.7", "port": "22", "protocol": "ssh",
        "ft_method": "sftp", "ft_port": "2222",
    }, follow_redirects=False)
    with session_factory() as s:
        h = s.execute(select(Host).where(Host.name == "ft-form")).scalar_one()
        assert h.protocol == "ssh" and h.ft_method == "sftp" and h.ft_port == 2222
        hid = h.id
    # Edit with no ft_method (as the form submits when "Not enabled") clears it.
    auth_client.post(f"/hosts/{hid}/edit", data={
        "name": "ft-form", "hostname": "10.0.0.7", "port": "22", "protocol": "ssh",
        "ft_method": "", "ft_port": "",
    }, follow_redirects=False)
    with session_factory() as s:
        h = s.get(Host, hid)
        assert h.ft_method is None and h.ft_port is None


def test_non_file_transfer_host_rejected(auth_client, session_factory, test_config):
    with session_factory() as s:
        from sqlalchemy import text
        tid = s.execute(text("SELECT id FROM tenants WHERE slug='default'")).scalar_one()
        host = Host(name="ssh-only", hostname="10.0.0.9", port=22, protocol="ssh", tenant_id=tid)
        s.add(host)
        s.commit()
        hid = host.id
    assert auth_client.get(f"/files/{hid}/list").status_code == 400


# ── backend: SFTP (direct) ───────────────────────────────────────────────────
def test_sftp_backend_roundtrip(session_factory, test_config, tmp_path):
    root = tmp_path / "sftproot"
    root.mkdir()
    (root / "hello.txt").write_bytes(b"served over sftp\n")

    async def run():
        srv, port = await start_sftp_server(root)
        try:
            hid = _make_host(session_factory, test_config, port=port, method="sftp",
                             username="sftpuser", password="sftppw", name="sftp-host")
            with session_factory() as s:
                backend = get_file_backend(test_config, s, s.get(Host, hid))
                names = [e.name for e in await backend.list_dir("/")]
                assert "hello.txt" in names
                buf = io.BytesIO()
                await backend.retrieve("/hello.txt", buf)
                assert buf.getvalue() == b"served over sftp\n"
                await backend.store("/up.bin", io.BytesIO(b"abc" * 10))
                assert (root / "up.bin").read_bytes() == b"abc" * 10
        finally:
            srv.close()

    asyncio.run(run())


# ── host-to-host: FTPS source to SFTP destination (server-side) ───────────────
def test_copy_between_hosts_ftps_to_sftp(session_factory, test_config, ftps, tmp_path):
    from dosm.ftp.service import transfer_between_hosts

    srv, _ = ftps  # FTPS host A serving readme.txt = b"hello from ftps\n"
    sftp_root = tmp_path / "sftproot"
    sftp_root.mkdir()

    async def run():
        sftp_srv, sftp_port = await start_sftp_server(sftp_root)
        try:
            a = _make_host(session_factory, test_config, port=srv.port, method="ftps",
                           username="ftpuser", password="ftppw", name="cp-src")
            b = _make_host(session_factory, test_config, port=sftp_port, method="sftp",
                           username="sftpuser", password="sftppw", name="cp-dst")
            with session_factory() as s:
                n = await transfer_between_hosts(
                    test_config, s, s.get(Host, a), "readme.txt",
                    s.get(Host, b), "landed.txt", move=False,
                )
            assert (sftp_root / "landed.txt").read_bytes() == b"hello from ftps\n"
            assert n == len(b"hello from ftps\n")
        finally:
            sftp_srv.close()

    asyncio.run(run())


def test_copy_route_and_targets(auth_client, session_factory, test_config, ftps, tmp_path):
    # FTPS source to SFTP destination. The SFTP server runs in its own thread so
    # the synchronous TestClient and the route handler's loop don't collide, and
    # we avoid two in-process TLS stacks at once.
    srv, root = ftps
    dst_root = tmp_path / "dstroot"
    dst_root.mkdir()
    sftp = ThreadedSftpServer(dst_root)
    sftp.start()
    try:
        a = _make_host(session_factory, test_config, port=srv.port, method="ftps",
                       username="ftpuser", password="ftppw", name="route-src")
        b = _make_host(session_factory, test_config, port=sftp.port, method="sftp",
                       username="sftpuser", password="sftppw", name="route-dst")
        # targets excludes the source host, includes the other host
        tj = auth_client.get(f"/files/{a}/targets").json()
        assert any(t["id"] == b and t["name"] == "route-dst" for t in tj["targets"])
        assert all(t["id"] != a for t in tj["targets"])
        # move readme.txt from A to B, then confirm it's gone from A's disk
        r = auth_client.post(f"/files/{a}/copy", data={
            "src": "readme.txt", "dst_host_id": b, "dst_dir": "", "move": "1",
        })
        assert r.status_code == 200 and r.json()["moved"] is True
        assert (dst_root / "readme.txt").read_bytes() == b"hello from ftps\n"
        assert not (root / "readme.txt").exists()
    finally:
        sftp.stop()


# ── backend: FTPS through an SSH jump (the headline capability) ───────────────
def test_ftps_through_ssh_jump(session_factory, test_config, ftps):
    srv, root = ftps

    # Fresh tunnel manager so its asyncio.Lock binds to this test's loop.
    import dosm.jumps.tunnels as _tunnels
    _tunnels._manager = None

    async def run():
        jump_srv, jump_port = await start_jump()
        try:
            with session_factory() as s:
                from sqlalchemy import text
                tid = s.execute(text("SELECT id FROM tenants WHERE slug='default'")).scalar_one()
                get_backend(test_config).set_str("ftp/test", "ftppw")
                cred = Credential(name="cred-jump", kind="login", username="ftpuser",
                                  secret_ref="ftp/test", tenant_id=tid)
                s.add(cred)
                jump = Host(name="jumpbox", hostname="127.0.0.1", port=jump_port,
                            protocol="ssh", is_jumpbox=True, tenant_id=tid)
                s.add(jump)
                s.flush()
                target = Host(name="ftps-jumped", hostname="127.0.0.1", port=22,
                              protocol="ssh", credential_id=cred.id, jump_host_id=jump.id,
                              ft_method="ftps", ft_port=srv.port, tenant_id=tid)
                s.add(target)
                s.commit()
                tid = target.id

            with session_factory() as s:
                backend = get_file_backend(test_config, s, s.get(Host, tid))
                names = [e.name for e in await backend.list_dir("")]
                assert "readme.txt" in names
                buf = io.BytesIO()
                await backend.retrieve("readme.txt", buf)
                assert buf.getvalue() == b"hello from ftps\n"
                await backend.store("viajump.bin", io.BytesIO(b"jump" * 8))
                assert (root / "viajump.bin").read_bytes() == b"jump" * 8
        finally:
            jump_srv.close()

    asyncio.run(run())
