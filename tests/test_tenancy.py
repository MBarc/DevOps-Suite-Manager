"""Phase 24a - multi-tenancy isolation.

Proves the core promise: a user in tenant A cannot see, fetch, or mutate tenant
B's data, names are unique only *within* a tenant, and a platform admin sees
across tenants but cannot write without an active tenant selected.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from dosm.auth.passwords import hash_password
from dosm.models import Credential, Host, Tenant, User


def _default_tid(s) -> int:
    return s.execute(text("SELECT id FROM tenants WHERE slug='default'")).scalar_one()


@pytest.fixture
def two_tenants(session_factory):
    """Default tenant (tid_a) + a second 'acme' tenant (tid_b)."""
    with session_factory() as s:
        tid_a = _default_tid(s)
        acme = s.execute(select(Tenant).where(Tenant.slug == "acme")).scalar_one_or_none()
        if acme is None:
            acme = Tenant(name="Acme", slug="acme", is_active=True)
            s.add(acme)
            s.commit()
            s.refresh(acme)
        tid_b = acme.id
    yield {"a": tid_a, "b": tid_b}
    # Teardown: remove the acme tenant (cascades to its rows) so the session
    # scoped DB stays clean for other modules.
    with session_factory() as s:
        s.execute(text("DELETE FROM tenants WHERE slug='acme'"))
        s.commit()


def _make_user(session_factory, username: str, role: str, tid: int | None) -> None:
    with session_factory() as s:
        existing = s.execute(
            select(User).where(User.username == username)
        ).scalar_one_or_none()
        if existing is not None:
            existing.role = role
            existing.tenant_id = tid
        else:
            s.add(User(
                username=username,
                password_hash=hash_password("testpass"),
                role=role,
                tenant_id=tid,
                is_active=True,
            ))
        s.commit()


def _login(app, username: str) -> TestClient:
    c = TestClient(app, raise_server_exceptions=True)
    r = c.post("/login", data={"username": username, "password": "testpass", "next": "/"},
               follow_redirects=False)
    assert r.status_code == 303, f"login failed for {username}: {r.status_code}"
    return c


@pytest.fixture
def alice(app, session_factory, two_tenants):
    """Tenant-A (Default) admin."""
    _make_user(session_factory, "alice", "admin", two_tenants["a"])
    return _login(app, "alice")


@pytest.fixture
def bob(app, session_factory, two_tenants):
    """Tenant-B (Acme) admin."""
    _make_user(session_factory, "bob", "admin", two_tenants["b"])
    return _login(app, "bob")


@pytest.fixture
def root(app, session_factory, two_tenants):
    """Platform admin (tenant-less)."""
    _make_user(session_factory, "root", "platform_admin", None)
    return _login(app, "root")


def _create_host(client: TestClient, name: str, hostname: str = "10.0.0.5") -> int | None:
    r = client.post("/hosts/new", data={"name": name, "hostname": hostname},
                    follow_redirects=False)
    if r.status_code != 303:
        return None
    # /hosts/<id>
    return int(r.headers["location"].rsplit("/", 1)[-1])


# ---------------------------------------------------------------------------
# Host isolation
# ---------------------------------------------------------------------------

def test_user_cannot_see_other_tenants_host(alice, bob, session_factory):
    a_host = _create_host(alice, "alpha-box")
    b_host = _create_host(bob, "beta-box")
    assert a_host is not None and b_host is not None

    # Alice's list shows her host, never Bob's.
    page = alice.get("/hosts?view=table").text
    assert "alpha-box" in page
    assert "beta-box" not in page

    # Direct fetch / edit / delete of Bob's host from Alice 404s (not 403 - we
    # don't even confirm existence).
    assert alice.get(f"/hosts/{b_host}", follow_redirects=False).status_code == 404
    assert alice.get(f"/hosts/{b_host}/edit", follow_redirects=False).status_code == 404
    assert alice.post(f"/hosts/{b_host}/delete", follow_redirects=False).status_code == 404


def test_host_name_unique_per_tenant_not_global(alice, bob, session_factory):
    # Same host name in two tenants is allowed.
    a = _create_host(alice, "samename")
    b = _create_host(bob, "samename")
    assert a is not None
    assert b is not None
    with session_factory() as s:
        rows = s.execute(select(Host).where(Host.name == "samename")).scalars().all()
        assert {h.tenant_id for h in rows} == {
            s.execute(text("SELECT id FROM tenants WHERE slug='default'")).scalar_one(),
            s.execute(text("SELECT id FROM tenants WHERE slug='acme'")).scalar_one(),
        }

    # But a duplicate within the SAME tenant is rejected.
    dup = alice.post("/hosts/new", data={"name": "samename", "hostname": "1.2.3.4"},
                     follow_redirects=False)
    assert dup.status_code == 400


# ---------------------------------------------------------------------------
# Credential isolation
# ---------------------------------------------------------------------------

def test_credentials_isolated_between_tenants(alice, bob, session_factory, two_tenants):
    # Seed one credential in each tenant directly.
    with session_factory() as s:
        s.add(Credential(name="a-cred", kind="login", username="u",
                         secret_ref="x", tenant_id=two_tenants["a"]))
        s.add(Credential(name="b-cred", kind="login", username="u",
                         secret_ref="y", tenant_id=two_tenants["b"]))
        s.commit()
    a_page = alice.get("/credentials").text
    assert "a-cred" in a_page
    assert "b-cred" not in a_page


# ---------------------------------------------------------------------------
# Platform admin
# ---------------------------------------------------------------------------

def test_platform_admin_sees_all_tenants(alice, bob, root):
    _create_host(alice, "alpha-box")
    _create_host(bob, "beta-box")
    # No active tenant selected => all-tenants read view: sees both.
    page = root.get("/hosts?view=table").text
    assert "alpha-box" in page
    assert "beta-box" in page


def test_platform_admin_cannot_create_without_active_tenant(root):
    # Writing into "all tenants" is undefined -> 403 until a tenant is selected.
    r = root.post("/hosts/new", data={"name": "nowhere", "hostname": "1.1.1.1"},
                  follow_redirects=False)
    assert r.status_code == 403
