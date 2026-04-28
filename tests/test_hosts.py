"""Host inventory CRUD route tests."""
from sqlalchemy import select

from dosm.models import AuditLog, Host


def _post_host(client, *, name="webserver01", hostname="10.0.0.1", protocol="ssh", port=22):
    return client.post(
        "/hosts/new",
        data={"name": name, "hostname": hostname, "port": port, "protocol": protocol},
        follow_redirects=False,
    )


def _get_host(session_factory, name: str) -> Host | None:
    with session_factory() as s:
        return s.execute(select(Host).where(Host.name == name)).scalar_one_or_none()


# ── Unauthenticated access ────────────────────────────────────────────────────


def test_hosts_list_requires_auth(anon_client):
    resp = anon_client.get("/hosts", follow_redirects=False)
    assert resp.status_code == 303


def test_hosts_new_requires_auth(anon_client):
    resp = anon_client.get("/hosts/new", follow_redirects=False)
    assert resp.status_code == 303


# ── Basic page rendering ──────────────────────────────────────────────────────


def test_hosts_list_returns_200(auth_client):
    resp = auth_client.get("/hosts")
    assert resp.status_code == 200


def test_hosts_new_form_returns_200(auth_client):
    resp = auth_client.get("/hosts/new")
    assert resp.status_code == 200


# ── Create ────────────────────────────────────────────────────────────────────


def test_create_host_redirects(auth_client):
    resp = _post_host(auth_client)
    assert resp.status_code == 303


def test_create_host_persists_in_db(auth_client, session_factory):
    _post_host(auth_client, name="persist-host", hostname="192.168.1.50")
    host = _get_host(session_factory, "persist-host")
    assert host is not None
    assert host.hostname == "192.168.1.50"
    assert host.protocol == "ssh"


def test_create_host_creates_audit_log(auth_client, session_factory):
    _post_host(auth_client, name="audit-host")
    with session_factory() as s:
        entry = s.execute(
            select(AuditLog).where(AuditLog.action == "host.create")
        ).scalar_one_or_none()
    assert entry is not None


def test_create_duplicate_host_does_not_crash(auth_client):
    _post_host(auth_client, name="dup-host")
    resp = _post_host(auth_client, name="dup-host")
    # Route should handle the IntegrityError and return a form/error, never a 5xx.
    assert resp.status_code < 500


# ── Read ──────────────────────────────────────────────────────────────────────


def test_host_detail_returns_200(auth_client, session_factory):
    _post_host(auth_client, name="detail-host")
    host = _get_host(session_factory, "detail-host")
    assert host is not None
    resp = auth_client.get(f"/hosts/{host.id}")
    assert resp.status_code == 200
    assert "detail-host" in resp.text


def test_host_detail_404_for_missing(auth_client):
    resp = auth_client.get("/hosts/99999")
    assert resp.status_code == 404


# ── Delete ────────────────────────────────────────────────────────────────────


def test_delete_host_redirects(auth_client, session_factory):
    _post_host(auth_client, name="del-host")
    host = _get_host(session_factory, "del-host")
    assert host is not None
    resp = auth_client.post(f"/hosts/{host.id}/delete", follow_redirects=False)
    assert resp.status_code == 303


def test_delete_host_removes_from_db(auth_client, session_factory):
    _post_host(auth_client, name="gone-host")
    host = _get_host(session_factory, "gone-host")
    assert host is not None
    host_id = host.id

    auth_client.post(f"/hosts/{host_id}/delete", follow_redirects=False)

    with session_factory() as s:
        gone = s.execute(select(Host).where(Host.id == host_id)).scalar_one_or_none()
    assert gone is None
