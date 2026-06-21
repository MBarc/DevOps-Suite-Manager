"""Pipeline payloads - saved input sets with shared/private visibility."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from dosm.auth.passwords import hash_password
from dosm.models import Pipeline, PipelinePayload, User
from dosm.pipelines import repo
from dosm.pipelines.inputs import validate_payload_values

SCHEMA = [
    {"name": "env", "type": "choice", "options": ["staging", "prod"], "required": True},
    {"name": "version", "type": "string"},
    {"name": "dry_run", "type": "boolean"},
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _default_tid(s):
    from sqlalchemy import text

    return s.execute(text("SELECT id FROM tenants WHERE slug='default'")).scalar_one()


def _ensure_user(session_factory, username: str, role: str) -> int:
    with session_factory() as s:
        u = s.execute(select(User).where(User.username == username)).scalar_one_or_none()
        if u is None:
            u = User(username=username, password_hash=hash_password("testpass"), role=role,
                     tenant_id=_default_tid(s), is_active=True)
            s.add(u)
            s.commit()
            s.refresh(u)
        elif u.role != role:
            u.role = role
            s.commit()
        return u.id


def _client(app, session_factory, username: str, role: str) -> TestClient:
    _ensure_user(session_factory, username, role)
    c = TestClient(app, raise_server_exceptions=True)
    r = c.post("/login", data={"username": username, "password": "testpass", "next": "/"},
               follow_redirects=False)
    assert r.status_code == 303
    return c


@pytest.fixture
def operator_client(app, session_factory):
    return _client(app, session_factory, "payop", "operator")


@pytest.fixture
def pipeline_id(session_factory):
    with session_factory() as s:
        p = Pipeline(
            name="deploy-app",
            provider="github_actions",
            config=json.dumps({"owner": "o", "repo": "r", "workflow": "w.yml", "ref": "main"}),
            inputs_schema=json.dumps(SCHEMA),
            tenant_id=_default_tid(s),
        )
        s.add(p)
        s.commit()
        s.refresh(p)
        return p.id


# ---------------------------------------------------------------------------
# Pure logic / repo
# ---------------------------------------------------------------------------

def test_validate_payload_values_drift():
    assert validate_payload_values(SCHEMA, {"env": "prod"}) == []
    # missing required
    assert any("env" in e for e in validate_payload_values(SCHEMA, {"version": "1"}))
    # bad choice
    assert any("env" in e for e in validate_payload_values(SCHEMA, {"env": "nope"}))
    # unknown key (schema changed under the payload)
    assert any("region" in e for e in validate_payload_values(SCHEMA, {"env": "prod", "region": "x"}))


def test_create_and_name_conflict(db, pipeline_id):
    repo.create_payload(db, pipeline_id=pipeline_id, name="Prod", values={"env": "prod"})
    db.commit()
    with pytest.raises(repo.PayloadNameConflict):
        repo.create_payload(db, pipeline_id=pipeline_id, name="Prod", values={"env": "prod"})


def test_copy_derives_unique_name(db, pipeline_id):
    p = repo.create_payload(db, pipeline_id=pipeline_id, name="Prod", values={"env": "prod"})
    db.commit()
    c1 = repo.copy_payload(db, p)
    c2 = repo.copy_payload(db, p)
    db.commit()
    names = {c1.name, c2.name}
    assert "Prod (copy)" in names
    assert len(names) == 2  # second copy got a distinct name


# ---------------------------------------------------------------------------
# Web CRUD + visibility
# ---------------------------------------------------------------------------

def test_operator_creates_payload_web(operator_client, pipeline_id, session_factory):
    r = operator_client.post(
        f"/pipelines/{pipeline_id}/payloads/new",
        data={"name": "Prod deploy", "description": "ship it", "visibility": "shared",
              "input__env": "prod", "input__version": "1.2.3", "input__dry_run": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    detail = operator_client.get(f"/pipelines/{pipeline_id}")
    assert "Prod deploy" in detail.text

    with session_factory() as s:
        pl = s.execute(select(PipelinePayload).where(PipelinePayload.name == "Prod deploy")).scalar_one()
        vals = json.loads(pl.values_json)
        assert vals == {"env": "prod", "version": "1.2.3", "dry_run": True}


def test_create_rejects_invalid_choice(operator_client, pipeline_id):
    r = operator_client.post(
        f"/pipelines/{pipeline_id}/payloads/new",
        data={"name": "Bad", "visibility": "shared", "input__env": "not-an-env"},
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "must be one of" in r.text


def test_viewer_cannot_create_payload(app, session_factory, pipeline_id):
    viewer = _client(app, session_factory, "payviewer", "viewer")
    r = viewer.post(
        f"/pipelines/{pipeline_id}/payloads/new",
        data={"name": "X", "visibility": "shared", "input__env": "prod"},
        follow_redirects=False,
    )
    assert r.status_code == 403


def test_private_payload_hidden_from_other_operator(app, session_factory, pipeline_id, operator_client, auth_client):
    # payop creates a PRIVATE payload
    r = operator_client.post(
        f"/pipelines/{pipeline_id}/payloads/new",
        data={"name": "My secret", "visibility": "private", "input__env": "prod"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    with session_factory() as s:
        pid = s.execute(select(PipelinePayload).where(PipelinePayload.name == "My secret")).scalar_one().id

    # a different operator can't see it on the page or open its edit form
    other = _client(app, session_factory, "payop2", "operator")
    assert "My secret" not in other.get(f"/pipelines/{pipeline_id}").text
    assert other.get(f"/pipelines/{pipeline_id}/payloads/{pid}/edit", follow_redirects=False).status_code == 404

    # owner can edit it; admin can too (audit)
    assert operator_client.get(f"/pipelines/{pipeline_id}/payloads/{pid}/edit", follow_redirects=False).status_code == 200
    assert auth_client.get(f"/pipelines/{pipeline_id}/payloads/{pid}/edit", follow_redirects=False).status_code == 200


def test_rename_copy_delete_web(operator_client, pipeline_id, session_factory):
    operator_client.post(
        f"/pipelines/{pipeline_id}/payloads/new",
        data={"name": "Orig", "visibility": "shared", "input__env": "staging"},
        follow_redirects=False,
    )
    with session_factory() as s:
        pid = s.execute(select(PipelinePayload).where(PipelinePayload.name == "Orig")).scalar_one().id

    # rename
    assert operator_client.post(f"/pipelines/{pipeline_id}/payloads/{pid}/rename",
                                data={"name": "Renamed"}, follow_redirects=False).status_code == 303
    # copy
    assert operator_client.post(f"/pipelines/{pipeline_id}/payloads/{pid}/copy",
                                follow_redirects=False).status_code == 303
    # delete
    assert operator_client.post(f"/pipelines/{pipeline_id}/payloads/{pid}/delete",
                                follow_redirects=False).status_code == 303

    with session_factory() as s:
        remaining = {pl.name for pl in s.execute(
            select(PipelinePayload).where(PipelinePayload.pipeline_id == pipeline_id)).scalars()}
    assert remaining == {"Renamed (copy)"}  # original renamed+deleted, copy survives
