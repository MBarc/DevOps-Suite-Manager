"""Settings → Access control: the AD/Okta group → role mapping editor + export."""
from __future__ import annotations

import csv
import io
import json

import pytest
from fastapi.testclient import TestClient

from dosm.auth.passwords import hash_password
from dosm.models import User


@pytest.fixture(autouse=True)
def _reset_rbac(test_config):
    """Keep each test isolated - the app's cfg.rbac is shared (session-scoped)."""
    saved = (dict(test_config.rbac.group_role_map), test_config.rbac.default_role)
    test_config.rbac.group_role_map = {}
    test_config.rbac.default_role = "viewer"
    yield
    test_config.rbac.group_role_map, test_config.rbac.default_role = saved


def _operator_client(app, session_factory):
    with session_factory() as s:
        from sqlalchemy import select
        if s.execute(select(User).where(User.username == "rbacop")).scalar_one_or_none() is None:
            s.add(User(username="rbacop", password_hash=hash_password("pw"), role="operator", is_active=True))
            s.commit()
    c = TestClient(app, raise_server_exceptions=True)
    c.post("/login", data={"username": "rbacop", "password": "pw", "next": "/"}, follow_redirects=False)
    return c


def test_add_update_delete_mapping(auth_client, test_config):
    # add
    r = auth_client.post("/settings/rbac/mapping",
                         data={"group": "DOSM-Admins", "role": "admin"}, follow_redirects=False)
    assert r.status_code == 303
    assert test_config.rbac.group_role_map == {"DOSM-Admins": "admin"}

    # update the same group's role (upsert)
    auth_client.post("/settings/rbac/mapping",
                     data={"group": "DOSM-Admins", "role": "operator"}, follow_redirects=False)
    assert test_config.rbac.group_role_map["DOSM-Admins"] == "operator"

    # delete
    r = auth_client.post("/settings/rbac/mapping/delete",
                         data={"group": "DOSM-Admins"}, follow_redirects=False)
    assert r.status_code == 303
    assert test_config.rbac.group_role_map == {}


def test_invalid_role_rejected(auth_client):
    r = auth_client.post("/settings/rbac/mapping",
                         data={"group": "G", "role": "superuser"}, follow_redirects=False)
    assert r.status_code == 400


def test_default_role_save(auth_client, test_config):
    r = auth_client.post("/settings/rbac/default",
                         data={"default_role": "operator"}, follow_redirects=False)
    assert r.status_code == 303
    assert test_config.rbac.default_role == "operator"


def test_default_role_none_allowed(auth_client, test_config):
    # "none" = deny unmapped users (require group membership); must be accepted.
    r = auth_client.post("/settings/rbac/default",
                         data={"default_role": "none"}, follow_redirects=False)
    assert r.status_code == 303
    assert test_config.rbac.default_role == "none"


def test_page_renders_and_lists_mappings(auth_client, test_config):
    test_config.rbac.group_role_map = {"DOSM-Ops": "operator"}
    page = auth_client.get("/settings/rbac")
    assert page.status_code == 200
    assert "DOSM-Ops" in page.text


def test_export_json(auth_client, test_config):
    test_config.rbac.group_role_map = {"DOSM-Admins": "admin", "DOSM-Ops": "operator"}
    test_config.rbac.default_role = "viewer"
    r = auth_client.get("/settings/rbac/export.json")
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    data = json.loads(r.text)
    assert data["default_role"] == "viewer"
    assert {"group": "DOSM-Admins", "role": "admin"} in data["groups"]
    assert len(data["groups"]) == 2


def test_export_csv(auth_client, test_config):
    test_config.rbac.group_role_map = {"DOSM-Admins": "admin"}
    test_config.rbac.default_role = "viewer"
    r = auth_client.get("/settings/rbac/export.csv")
    assert r.status_code == 200
    rows = list(csv.reader(io.StringIO(r.text)))
    assert rows[0] == ["group", "role"]
    assert ["DOSM-Admins", "admin"] in rows
    assert any(row[1] == "viewer" and "default" in row[0] for row in rows)


def test_rbac_settings_admin_only(app, session_factory):
    op = _operator_client(app, session_factory)
    assert op.get("/settings/rbac", follow_redirects=False).status_code == 403
    assert op.post("/settings/rbac/mapping",
                   data={"group": "X", "role": "admin"}, follow_redirects=False).status_code == 403
    assert op.get("/settings/rbac/export.csv", follow_redirects=False).status_code == 403
