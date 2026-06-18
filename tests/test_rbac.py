"""RBAC Phase A - role gates, private credentials, per-user data scoping."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from dosm.auth.passwords import hash_password
from dosm.models import Credential, User


def _ensure_user(session_factory, username: str, role: str) -> int:
    """Create (or fetch) a local user with a known password 'testpass'."""
    from sqlalchemy import select

    with session_factory() as s:
        u = s.execute(select(User).where(User.username == username)).scalar_one_or_none()
        if u is None:
            u = User(
                username=username,
                password_hash=hash_password("testpass"),
                role=role,
                is_active=True,
            )
            s.add(u)
            s.commit()
            s.refresh(u)
        elif u.role != role:
            u.role = role
            s.commit()
        return u.id


def _client_for(app, session_factory, username: str, role: str) -> TestClient:
    _ensure_user(session_factory, username, role)
    c = TestClient(app, raise_server_exceptions=True)
    resp = c.post(
        "/login",
        data={"username": username, "password": "testpass", "next": "/"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, f"login failed for {username}: {resp.status_code}"
    return c


@pytest.fixture
def operator_client(app, session_factory):
    return _client_for(app, session_factory, "testop", "operator")


@pytest.fixture
def viewer_client(app, session_factory):
    return _client_for(app, session_factory, "testviewer", "viewer")


# ---------------------------------------------------------------------------
# Role gate matrix
# ---------------------------------------------------------------------------

def test_settings_requires_admin(auth_client, operator_client, viewer_client):
    # admin gets in
    assert auth_client.get("/settings", follow_redirects=False).status_code == 200
    # operator + viewer are forbidden
    assert operator_client.get("/settings", follow_redirects=False).status_code == 403
    assert viewer_client.get("/settings", follow_redirects=False).status_code == 403


def test_terminals_require_admin(operator_client, viewer_client):
    # terminals are admin-only regardless of enabled flag; non-admins get 403
    # (or 404 if terminals are disabled in the test config - never a 200).
    for client in (operator_client, viewer_client):
        code = client.get("/terminals", follow_redirects=False).status_code
        assert code in (403, 404)


def test_host_mutation_requires_operator(viewer_client, operator_client):
    # viewer cannot create a host
    r = viewer_client.post(
        "/hosts/new",
        data={"name": "v-box", "hostname": "10.0.0.9"},
        follow_redirects=False,
    )
    assert r.status_code == 403
    # operator can (303 redirect on success)
    r = operator_client.post(
        "/hosts/new",
        data={"name": "op-box", "hostname": "10.0.0.10"},
        follow_redirects=False,
    )
    assert r.status_code == 303


def test_viewer_can_read_hosts(viewer_client):
    assert viewer_client.get("/hosts", follow_redirects=False).status_code == 200


# ---------------------------------------------------------------------------
# Private vs shared credentials
# ---------------------------------------------------------------------------

def _make_credential(session_factory, owner_id: int, visibility: str) -> int:
    with session_factory() as s:
        cred = Credential(
            name=f"cred-{visibility}-{owner_id}",
            kind="login",
            username="svc",
            secret_ref=f"credentials/test-{visibility}-{owner_id}",
            owner_id=owner_id,
            visibility=visibility,
        )
        s.add(cred)
        s.commit()
        s.refresh(cred)
        return cred.id


def test_private_credential_hidden_from_other_users(
    app, session_factory, auth_client, operator_client
):
    op_id = _ensure_user(session_factory, "testop", "operator")
    cid = _make_credential(session_factory, owner_id=op_id, visibility="private")

    # A different operator cannot see it.
    other = _client_for(app, session_factory, "testop2", "operator")
    assert other.get(f"/credentials/{cid}", follow_redirects=False).status_code == 404
    listing = other.get("/credentials", follow_redirects=False)
    assert f"cred-private-{op_id}" not in listing.text

    # The owner can see it.
    assert operator_client.get(f"/credentials/{cid}", follow_redirects=False).status_code == 200
    assert f"cred-private-{op_id}" in operator_client.get("/credentials").text

    # An admin can see it (audit).
    assert auth_client.get(f"/credentials/{cid}", follow_redirects=False).status_code == 200


def test_shared_credential_visible_to_everyone(app, session_factory, operator_client):
    op_id = _ensure_user(session_factory, "testop", "operator")
    cid = _make_credential(session_factory, owner_id=op_id, visibility="shared")
    other = _client_for(app, session_factory, "testop3", "operator")
    assert other.get(f"/credentials/{cid}", follow_redirects=False).status_code == 200


# ---------------------------------------------------------------------------
# Per-user data scoping - conversations
# ---------------------------------------------------------------------------

def test_conversation_private_to_owner(app, session_factory, operator_client):
    # operator creates a conversation
    r = operator_client.post("/chat/new", follow_redirects=False)
    assert r.status_code == 303
    location = r.headers["location"]  # /chat/{cid}
    cid = location.rstrip("/").split("/")[-1]

    # a different user cannot open it (redirect away or 404, never 200)
    other = _client_for(app, session_factory, "testop4", "operator")
    code = other.get(f"/chat/{cid}", follow_redirects=False).status_code
    assert code in (303, 404)


# ---------------------------------------------------------------------------
# Break-glass local login
# ---------------------------------------------------------------------------

def test_local_login_still_works(auth_client):
    # auth_client logging in is the break-glass local path; confirm it reaches
    # an authenticated page.
    assert auth_client.get("/hosts", follow_redirects=False).status_code == 200
