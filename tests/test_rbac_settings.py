"""Settings -> Access control: the AD/Okta group -> (tenant, role) mapping
editor + export. Phase 24b moved the mapping from config.yaml into the
tenant-scoped group_mappings table."""
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


def _seed_mapping(session_factory, group: str, role: str) -> None:
    with session_factory() as s:
        s.add(GroupMapping(group_name=group, tenant_id=_default_tid(s), role=role))
        s.commit()


def _mappings(session_factory) -> dict[str, str]:
    with session_factory() as s:
        return {m.group_name: m.role
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


def test_add_update_delete_mapping(auth_client, session_factory):
    # add (tenant admin -> their Default tenant, no tenant_id needed)
    r = auth_client.post("/settings/rbac/mapping",
                         data={"group": "DOSM-Admins", "role": "admin"}, follow_redirects=False)
    assert r.status_code == 303
    assert _mappings(session_factory) == {"DOSM-Admins": "admin"}

    # update the same group's role (upsert)
    auth_client.post("/settings/rbac/mapping",
                     data={"group": "DOSM-Admins", "role": "operator"}, follow_redirects=False)
    assert _mappings(session_factory)["DOSM-Admins"] == "operator"

    # delete (by mapping id)
    with session_factory() as s:
        mid = s.execute(
            select(GroupMapping).where(GroupMapping.group_name == "DOSM-Admins")
        ).scalar_one().id
    r = auth_client.post("/settings/rbac/mapping/delete",
                         data={"mapping_id": mid}, follow_redirects=False)
    assert r.status_code == 303
    assert _mappings(session_factory) == {}


def test_invalid_role_rejected(auth_client):
    # superuser is not a role at all -> 400
    r = auth_client.post("/settings/rbac/mapping",
                         data={"group": "G", "role": "superuser"}, follow_redirects=False)
    assert r.status_code == 400
    # platform_admin IS a role, but a tenant admin may not grant it -> 403
    r = auth_client.post("/settings/rbac/mapping",
                         data={"group": "G", "role": "platform_admin"}, follow_redirects=False)
    assert r.status_code == 403


def test_default_role_requires_platform_admin(auth_client, app, session_factory, test_config):
    # A tenant admin cannot change the global unmapped-user default.
    r = auth_client.post("/settings/rbac/default",
                         data={"default_role": "operator"}, follow_redirects=False)
    assert r.status_code == 403
    # A platform admin can.
    pa = _platform_admin_client(app, session_factory)
    r = pa.post("/settings/rbac/default",
                data={"default_role": "operator"}, follow_redirects=False)
    assert r.status_code == 303
    assert test_config.rbac.default_role == "operator"


def test_default_role_none_allowed(app, session_factory, test_config):
    pa = _platform_admin_client(app, session_factory)
    r = pa.post("/settings/rbac/default",
                data={"default_role": "none"}, follow_redirects=False)
    assert r.status_code == 303
    assert test_config.rbac.default_role == "none"


def test_page_renders_and_lists_mappings(auth_client, session_factory):
    _seed_mapping(session_factory, "DOSM-Ops", "operator")
    page = auth_client.get("/settings/rbac")
    assert page.status_code == 200
    assert "DOSM-Ops" in page.text


def test_export_json(auth_client, session_factory):
    _seed_mapping(session_factory, "DOSM-Admins", "admin")
    _seed_mapping(session_factory, "DOSM-Ops", "operator")
    r = auth_client.get("/settings/rbac/export.json")
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    data = json.loads(r.text)
    assert data["default_role"] == "viewer"
    groups = [{"group": g["group"], "role": g["role"]} for g in data["groups"]]
    assert {"group": "DOSM-Admins", "role": "admin"} in groups
    assert len(data["groups"]) == 2


def test_export_csv(auth_client, session_factory):
    _seed_mapping(session_factory, "DOSM-Admins", "admin")
    r = auth_client.get("/settings/rbac/export.csv")
    assert r.status_code == 200
    rows = list(csv.reader(io.StringIO(r.text)))
    assert rows[0] == ["group", "tenant", "role"]
    assert any(row[0] == "DOSM-Admins" and row[2] == "admin" for row in rows)
    assert any("default" in row[0] for row in rows)


def test_platform_admin_can_grant_platform_admin(app, session_factory):
    pa = _platform_admin_client(app, session_factory)
    r = pa.post("/settings/rbac/mapping",
                data={"group": "DOSM-Platform", "role": "platform_admin"},
                follow_redirects=False)
    assert r.status_code == 303
    with session_factory() as s:
        m = s.execute(
            select(GroupMapping).where(GroupMapping.group_name == "DOSM-Platform")
        ).scalar_one()
        assert m.role == "platform_admin"
        assert m.tenant_id is None  # tenant-less grant


def test_rbac_settings_admin_only(app, session_factory):
    op = _operator_client(app, session_factory)
    assert op.get("/settings/rbac", follow_redirects=False).status_code == 403
    assert op.post("/settings/rbac/mapping",
                   data={"group": "X", "role": "admin"}, follow_redirects=False).status_code == 403
    assert op.get("/settings/rbac/export.csv", follow_redirects=False).status_code == 403
