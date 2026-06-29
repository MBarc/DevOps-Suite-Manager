"""Org-tree explorer for Pipelines / Credentials / File transfer: the
``org_unit_id`` assignment endpoints, visibility-aware folder counts, and that
the explorer pages render."""
from __future__ import annotations

from dosm.applications import repo as org_repo
from dosm.models import Credential, Pipeline, User


def _app_unit(db, tid, name="Payments"):
    u = org_repo.create_unit(db, tenant_id=tid, name=name, tier="application", parent_id=None)
    db.commit()
    return u


# ── assign-org endpoints ─────────────────────────────────────────────────────


def test_pipeline_assign_org(auth_client, db, default_tenant):
    tid = default_tenant["id"]
    u = _app_unit(db, tid)
    p = Pipeline(tenant_id=tid, name="deploy", provider="github_actions",
                 config="{}", visibility="shared")
    db.add(p)
    db.commit()

    r = auth_client.post(f"/pipelines/{p.id}/assign-org", data={"org_unit_id": str(u.id)})
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] and j["org_unit_id"] == u.id and j["path"] == "Payments"
    db.refresh(p)
    assert p.org_unit_id == u.id

    # empty org_unit_id clears the assignment
    r = auth_client.post(f"/pipelines/{p.id}/assign-org", data={"org_unit_id": ""})
    j = r.json()
    assert j["ok"] and j["org_unit_id"] is None and j["path"] is None
    db.refresh(p)
    assert p.org_unit_id is None

    # an org unit that doesn't exist (or is another tenant's) is rejected
    r = auth_client.post(f"/pipelines/{p.id}/assign-org", data={"org_unit_id": "999999"})
    assert r.status_code == 400 and r.json()["ok"] is False


def test_credential_assign_org(auth_client, db, default_tenant):
    tid = default_tenant["id"]
    u = _app_unit(db, tid, name="Infra")
    c = Credential(tenant_id=tid, name="svc", kind="login", secret_ref="x", visibility="shared")
    db.add(c)
    db.commit()

    r = auth_client.post(f"/credentials/{c.id}/assign-org", data={"org_unit_id": str(u.id)})
    assert r.status_code == 200
    assert r.json()["ok"] and r.json()["path"] == "Infra"
    db.refresh(c)
    assert c.org_unit_id == u.id

    r = auth_client.post(f"/credentials/{c.id}/assign-org", data={"org_unit_id": "999999"})
    assert r.status_code == 400 and r.json()["ok"] is False


# ── visibility-aware folder counts ───────────────────────────────────────────


def test_pipeline_counts_respect_visibility(db, default_tenant):
    from dosm.pipelines.access import visible_pipelines_filter

    tid = default_tenant["id"]
    u = _app_unit(db, tid, name="App")
    owner = User(username="ownerx", password_hash="x", role="operator",
                 tenant_id=tid, is_active=True)
    db.add(owner)
    db.flush()
    db.add(Pipeline(tenant_id=tid, name="priv", provider="github_actions", config="{}",
                    visibility="private", owner_id=owner.id, org_unit_id=u.id))
    db.add(Pipeline(tenant_id=tid, name="shared", provider="github_actions", config="{}",
                    visibility="shared", org_unit_id=u.id))
    db.commit()

    other = User(username="otherx", password_hash="x", role="operator",
                 tenant_id=tid, is_active=True)
    db.add(other)
    db.flush()

    # A non-owner operator only counts the shared pipeline in the folder.
    visible = org_repo.direct_counts(db, tid, Pipeline, extra=visible_pipelines_filter(other))
    assert visible.get(u.id) == 1
    # No filter (admin view) counts both.
    assert org_repo.direct_counts(db, tid, Pipeline).get(u.id) == 2


# ── explorer pages render ────────────────────────────────────────────────────


def test_explorer_pages_render(auth_client):
    for path in ("/pipelines?view=explorer", "/credentials?view=explorer", "/files?view=explorer"):
        r = auth_client.get(path)
        assert r.status_code == 200, f"{path} -> {r.status_code}"
        assert 'class="ex-tree"' in r.text


def test_inventory_page_renders(auth_client, db, default_tenant):
    tid = default_tenant["id"]
    # one of each type so all three card kinds + type pills render
    db.add(Pipeline(tenant_id=tid, name="inv-pipe", provider="github_actions",
                    config="{}", visibility="shared"))
    db.add(Credential(tenant_id=tid, name="inv-cred", kind="login", secret_ref="x",
                      visibility="shared"))
    db.commit()
    r = auth_client.get("/inventory")
    assert r.status_code == 200
    assert 'class="ex-tree"' in r.text
    assert 'ex-type-toggle' in r.text          # the Hosts/Pipelines/Credentials filter pills
    assert 'data-type="pipeline"' in r.text    # the seeded pipeline card
    assert 'data-type="credential"' in r.text  # the seeded credential card
