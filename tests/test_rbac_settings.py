"""Settings -> Access control: the AD/Okta group -> tenant mapping editor +
export. Phase 24b moved the mapping from config.yaml into the tenant-scoped
group_mappings table; the access-control rework then dropped per-group roles -
every mapped group now grants only the baseline ``viewer`` within its tenant,
and per-user elevation lives in the Members page."""
from __future__ import annotations

import csv
import io
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from dosm.auth.passwords import hash_password
from dosm.models import GroupMapping, User


@pytest.fixture(autouse=True)
def _reset_rbac(test_config):
    """Keep each test isolated - the app's cfg.rbac.default_role is shared
    (session-scoped). Group mappings now live in the DB (wiped by clean_tables)."""
    saved = test_config.rbac.default_role
    test_config.rbac.default_role = "viewer"
    yield
    test_config.rbac.default_role = saved


def _default_tid(s) -> int:
    return s.execute(text("SELECT id FROM tenants WHERE slug='default'")).scalar_one()


def _seed_mapping(session_factory, group: str, *, tenant_id: int | None = None) -> None:
    with session_factory() as s:
        tid = _default_tid(s) if tenant_id is None else tenant_id
        s.add(GroupMapping(group_name=group, tenant_id=tid, role="viewer"))
        s.commit()


def _mappings(session_factory) -> dict[str, tuple[int | None, str]]:
    with session_factory() as s:
        return {m.group_name: (m.tenant_id, m.role)
                for m in s.execute(select(GroupMapping)).scalars()}


def _user_client(app, session_factory, username: str, role: str, *, tenant: bool = True):
    with session_factory() as s:
        if s.execute(select(User).where(User.username == username)).scalar_one_or_none() is None:
            tid = _default_tid(s) if tenant else None
            s.add(User(username=username, password_hash=hash_password("pw"), role=role,
                       tenant_id=tid, is_active=True))
            s.commit()
    c = TestClient(app, raise_server_exceptions=True)
    c.post("/login", data={"username": username, "password": "pw", "next": "/"},
           follow_redirects=False)
    return c


def _operator_client(app, session_factory):
    return _user_client(app, session_factory, "rbacop", "operator")


def _platform_admin_client(app, session_factory):
    return _user_client(app, session_factory, "rbacplatform", "platform_admin", tenant=False)


def test_add_and_delete_mapping_grants_viewer(auth_client, session_factory):
    # A tenant admin adds a group -> their Default tenant. No role is chosen;
    # the grant is always the baseline viewer.
    r = auth_client.post("/settings/rbac/mapping",
                         data={"group": "DOSM-Team"}, follow_redirects=False)
    assert r.status_code == 303
    assert _mappings(session_factory)["DOSM-Team"][1] == "viewer"

    # Re-adding the same group is an idempotent upsert (still one row, viewer).
    auth_client.post("/settings/rbac/mapping",
                     data={"group": "DOSM-Team"}, follow_redirects=False)
    assert list(_mappings(session_factory)) == ["DOSM-Team"]

    with session_factory() as s:
        mid = s.execute(
            select(GroupMapping).where(GroupMapping.group_name == "DOSM-Team")
        ).scalar_one().id
    r = auth_client.post("/settings/rbac/mapping/delete",
                         data={"mapping_id": mid}, follow_redirects=False)
    assert r.status_code == 303
    assert _mappings(session_factory) == {}


def test_role_field_is_ignored(auth_client, session_factory):
    # Even if a client posts a role (old form), the stored grant is viewer:
    # group membership never confers elevated permissions.
    auth_client.post("/settings/rbac/mapping",
                     data={"group": "G", "role": "tenant_admin"}, follow_redirects=False)
    assert _mappings(session_factory)["G"][1] == "viewer"


def test_blank_group_rejected(auth_client):
    r = auth_client.post("/settings/rbac/mapping",
                         data={"group": "   "}, follow_redirects=False)
    assert r.status_code == 400


def test_default_role_requires_platform_admin(auth_client, app, session_factory, test_config):
    # A tenant admin cannot change the global unmapped-user default.
    r = auth_client.post("/settings/rbac/default",
                         data={"default_role": "viewer"}, follow_redirects=False)
    assert r.status_code == 403
    # A platform admin can.
    pa = _platform_admin_client(app, session_factory)
    r = pa.post("/settings/rbac/default",
                data={"default_role": "viewer"}, follow_redirects=False)
    assert r.status_code == 303
    assert test_config.rbac.default_role == "viewer"


def test_default_role_none_allowed(app, session_factory, test_config):
    pa = _platform_admin_client(app, session_factory)
    r = pa.post("/settings/rbac/default",
                data={"default_role": "none"}, follow_redirects=False)
    assert r.status_code == 303
    assert test_config.rbac.default_role == "none"


def test_default_role_elevated_rejected(app, session_factory):
    # The unmapped-user default can only be none or viewer now - never an
    # elevated tenant role.
    pa = _platform_admin_client(app, session_factory)
    r = pa.post("/settings/rbac/default",
                data={"default_role": "tenant_admin"}, follow_redirects=False)
    assert r.status_code == 400


def test_page_renders_and_lists_mappings(auth_client, session_factory):
    _seed_mapping(session_factory, "DOSM-Ops")
    page = auth_client.get("/settings/rbac")
    assert page.status_code == 200
    assert "DOSM-Ops" in page.text


def test_export_json(auth_client, session_factory):
    _seed_mapping(session_factory, "DOSM-Team")
    _seed_mapping(session_factory, "DOSM-Ops")
    r = auth_client.get("/settings/rbac/export.json")
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    data = json.loads(r.text)
    assert data["default_role"] == "viewer"
    names = {g["group"] for g in data["groups"]}
    assert names == {"DOSM-Team", "DOSM-Ops"}


def test_export_csv(auth_client, session_factory):
    _seed_mapping(session_factory, "DOSM-Team")
    r = auth_client.get("/settings/rbac/export.csv")
    assert r.status_code == 200
    rows = list(csv.reader(io.StringIO(r.text)))
    assert rows[0] == ["group", "tenant", "grants"]
    assert any(row[0] == "DOSM-Team" and row[2] == "viewer" for row in rows)
    assert any("default" in row[0] for row in rows)


def test_platform_admin_maps_group_into_named_tenant(app, session_factory):
    pa = _platform_admin_client(app, session_factory)
    with session_factory() as s:
        tid = _default_tid(s)
    r = pa.post("/settings/rbac/mapping",
                data={"group": "DOSM-Platform", "tenant_id": str(tid)},
                follow_redirects=False)
    assert r.status_code == 303
    assert _mappings(session_factory)["DOSM-Platform"] == (tid, "viewer")


def test_platform_admin_must_name_a_tenant(app, session_factory):
    # A platform admin has no implicit tenant, so omitting tenant_id is a 400.
    pa = _platform_admin_client(app, session_factory)
    r = pa.post("/settings/rbac/mapping",
                data={"group": "DOSM-Platform"}, follow_redirects=False)
    assert r.status_code == 400


def test_rbac_settings_admin_only(app, session_factory):
    op = _operator_client(app, session_factory)
    assert op.get("/settings/rbac", follow_redirects=False).status_code == 403
    assert op.post("/settings/rbac/mapping",
                   data={"group": "X"}, follow_redirects=False).status_code == 403
    assert op.get("/settings/rbac/export.csv", follow_redirects=False).status_code == 403
