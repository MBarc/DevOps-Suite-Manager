"""Dynamic (per-user / PIM) credentials: per-user secret storage, connecting-user
resolution, the resolver chokepoints, the provisioning gate, and the web flow."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from dosm.credentials.dynamic import (
    DynamicCredentialError,
    clear_user_material,
    connecting_user,
    get_user_material,
    has_user_material,
    resolve_dynamic,
    set_user_material,
)
from dosm.models import Credential
from dosm.secrets import SecretNotFound


class _FakeBackend:
    """In-memory secrets backend so the dynamic-credential logic can be tested
    without the Fernet/DB backend."""

    def __init__(self):
        self.store: dict[str, bytes] = {}

    def get(self, path):
        if path not in self.store:
            raise SecretNotFound(path)
        return self.store[path]

    def set(self, path, value):
        self.store[path] = value

    def delete(self, path):
        if path not in self.store:
            raise SecretNotFound(path)
        del self.store[path]

    def list(self, prefix=""):
        return [p for p in self.store if p.startswith(prefix)]

    def get_str(self, path):
        return self.get(path).decode("utf-8")

    def set_str(self, path, value):
        self.set(path, value.encode("utf-8"))


@pytest.fixture
def fake_backend(monkeypatch):
    fb = _FakeBackend()
    monkeypatch.setattr("dosm.credentials.dynamic.get_backend", lambda cfg=None: fb)
    return fb


def _dyn_cred():
    return Credential(id=1, tenant_id=1, name="pim", kind="dynamic",
                      secret_ref="t/default/credentials/pim", visibility="shared")


# ── per-user material + resolution ───────────────────────────────────────────


def test_material_roundtrip_is_per_user(test_config, fake_backend):
    c = _dyn_cred()
    assert get_user_material(test_config, c, 7) is None
    assert has_user_material(test_config, c, 7) is False
    set_user_material(test_config, c, 7, "alice", "pw1")
    assert get_user_material(test_config, c, 7) == ("alice", "pw1")
    assert has_user_material(test_config, c, 7) is True
    assert get_user_material(test_config, c, 8) is None     # other user independent
    clear_user_material(test_config, c, 7)
    assert get_user_material(test_config, c, 7) is None


def test_resolve_dynamic_picks_connecting_user(test_config, fake_backend):
    c = _dyn_cred()
    set_user_material(test_config, c, 7, "alice", "pw1")
    set_user_material(test_config, c, 8, "bob", "pw2")
    with connecting_user(7):
        assert resolve_dynamic(test_config, c) == ("alice", "pw1")
    with connecting_user(8):
        assert resolve_dynamic(test_config, c) == ("bob", "pw2")


def test_resolve_dynamic_requires_connecting_user(test_config, fake_backend):
    with pytest.raises(DynamicCredentialError):
        resolve_dynamic(test_config, _dyn_cred())  # no connecting user in context


def test_resolve_dynamic_requires_provisioned(test_config, fake_backend):
    with connecting_user(99), pytest.raises(DynamicCredentialError):
        resolve_dynamic(test_config, _dyn_cred())  # user 99 hasn't stored theirs


# ── resolver chokepoints honour dynamic ──────────────────────────────────────


def test_ftp_credential_material_dynamic(test_config, fake_backend):
    from dosm.ftp.service import credential_material

    c = _dyn_cred()
    set_user_material(test_config, c, 7, "alice", "pw1")
    with connecting_user(7):
        assert credential_material(test_config, c) == ("alice", "pw1", None)


def test_guac_resolve_credential_dynamic(test_config, fake_backend):
    from dosm.guacamole.builder import _resolve_credential

    c = _dyn_cred()
    set_user_material(test_config, c, 7, "alice", "pw1")
    with connecting_user(7):
        assert _resolve_credential(test_config, c) == ("alice", "pw1", None, None)


# ── provisioning gate ────────────────────────────────────────────────────────


def test_gate_blocks_unprovisioned(test_config, fake_backend):
    from types import SimpleNamespace

    from dosm.credentials.access import first_unprovisioned_dynamic

    c = _dyn_cred()
    user = SimpleNamespace(id=7)
    assert first_unprovisioned_dynamic(test_config, user, [c]) is c
    set_user_material(test_config, c, 7, "alice", "pw1")
    assert first_unprovisioned_dynamic(test_config, user, [c]) is None


# ── web flow ─────────────────────────────────────────────────────────────────


def test_create_dynamic_stores_no_shared_secret(auth_client, db, test_config):
    resp = auth_client.post(
        "/credentials/new",
        data={"name": "pim-web", "kind": "dynamic", "username": "",
              "secret_value": "should-not-store", "visibility": "shared"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    cred = db.execute(select(Credential).where(Credential.name == "pim-web")).scalar_one()
    assert cred.kind == "dynamic"
    from dosm.secrets import get_backend
    with pytest.raises(SecretNotFound):
        get_backend(test_config).get(cred.secret_ref)


def test_my_credentials_set_and_clear(auth_client, db, default_tenant, admin_user, test_config):
    db.add(Credential(tenant_id=default_tenant["id"], name="pim-mine", kind="dynamic",
                      secret_ref="t/default/credentials/pim-mine", visibility="shared"))
    db.commit()
    cred = db.execute(select(Credential).where(Credential.name == "pim-mine")).scalar_one()

    page = auth_client.get("/credentials/mine")
    assert page.status_code == 200 and "pim-mine" in page.text

    r = auth_client.post(f"/credentials/mine/{cred.id}",
                         data={"username": "alice", "password": "pw"}, follow_redirects=False)
    assert r.status_code == 303
    assert get_user_material(test_config, cred, admin_user["id"]) == ("alice", "pw")

    r = auth_client.post(f"/credentials/mine/{cred.id}/clear", follow_redirects=False)
    assert r.status_code == 303
    assert get_user_material(test_config, cred, admin_user["id"]) is None
