"""End-to-end pipeline form / typed-inputs smoke.

Exercises both halves of the typed-inputs feature against the running app:

  1. Pipeline create form accepts row-based schema fields and persists them.
  2. Pipeline detail page renders typed inputs (text/checkbox/choice).
  3. POST /run with valid typed inputs creates a run.
  4. POST /run with a bad choice value is rejected with 400 + error banner.
  5. Pipelines without a schema still accept the legacy free-form textarea.

We don't need a real GitHub endpoint - `trigger_pipeline` traps adapter
failures into a PipelineRun row with status='failed', which is exactly the
path we want to verify here.
"""
from __future__ import annotations

import json


def _make_pipeline_with_schema(auth_client) -> int:
    resp = auth_client.post(
        "/pipelines/new",
        data={
            "name": "deploy-test",
            "provider": "github_actions",
            "description": "smoke",
            "credential_id": "",
            "gh_owner": "acme",
            "gh_repo": "app",
            "gh_workflow": "deploy.yml",
            "gh_ref": "main",
            "gh_api_base": "https://api.github.com",
            # Row 0: required choice
            "schema_name__0": "environment",
            "schema_type__0": "choice",
            "schema_options__0": "dev, staging, prod",
            "schema_default__0": "staging",
            "schema_required__0": "1",
            # Row 1: boolean
            "schema_name__1": "dry_run",
            "schema_type__1": "boolean",
            # Row 2: string
            "schema_name__2": "version",
            "schema_type__2": "string",
            "schema_default__2": "1.0.0",
            # Row 3: blank - should be dropped
            "schema_name__3": "",
            "schema_type__3": "string",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    location = resp.headers["location"]
    assert location.startswith("/pipelines/")
    return int(location.split("/")[-1])


def test_create_pipeline_persists_typed_schema(auth_client, db):
    pid = _make_pipeline_with_schema(auth_client)
    from dosm.models import Pipeline

    p = db.get(Pipeline, pid)
    schema = json.loads(p.inputs_schema)
    names = [r["name"] for r in schema]
    assert names == ["environment", "dry_run", "version"]
    env_row = schema[0]
    assert env_row["type"] == "choice"
    assert env_row["options"] == ["dev", "staging", "prod"]
    assert env_row["default"] == "staging"
    assert env_row["required"] is True
    assert schema[1]["type"] == "boolean"
    assert schema[2]["default"] == "1.0.0"


def test_pipeline_detail_renders_typed_form(auth_client):
    pid = _make_pipeline_with_schema(auth_client)
    resp = auth_client.get(f"/pipelines/{pid}")
    assert resp.status_code == 200
    body = resp.text
    # choice rendered as <select>
    assert 'name="input__environment"' in body
    assert "<option value=\"staging\" selected" in body
    # boolean rendered as checkbox
    assert 'type="checkbox"' in body
    assert 'name="input__dry_run"' in body
    # string rendered as text input
    assert 'type="text" name="input__version"' in body
    assert 'value="1.0.0"' in body


def test_run_with_invalid_choice_returns_400(auth_client):
    pid = _make_pipeline_with_schema(auth_client)
    resp = auth_client.post(
        f"/pipelines/{pid}/run",
        data={
            "input__environment": "qa",  # not in options
            "input__version": "1.2.3",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "must be one of" in resp.text
    assert "environment" in resp.text


def test_run_missing_required_returns_400(auth_client):
    pid = _make_pipeline_with_schema(auth_client)
    resp = auth_client.post(
        f"/pipelines/{pid}/run",
        data={"input__version": "1.2.3"},  # environment is required, omitted
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "environment" in resp.text
    assert "required" in resp.text


def test_run_with_valid_inputs_creates_run(auth_client, db):
    pid = _make_pipeline_with_schema(auth_client)
    resp = auth_client.post(
        f"/pipelines/{pid}/run",
        data={
            "input__environment": "prod",
            "input__dry_run": "1",
            "input__version": "2.0.0",
        },
        follow_redirects=False,
    )
    # Either 303 to the run detail (no creds to fails inside trigger_pipeline,
    # but the run row still gets persisted with status='failed').
    assert resp.status_code == 303
    assert "/pipelines/runs/" in resp.headers["location"]

    from dosm.models import PipelineRun

    runs = list(db.query(PipelineRun).filter_by(pipeline_id=pid).all())
    assert len(runs) == 1
    stored = json.loads(runs[0].inputs)
    assert stored == {"environment": "prod", "dry_run": True, "version": "2.0.0"}


def _make_ado_pipeline(auth_client) -> int:
    resp = auth_client.post(
        "/pipelines/new",
        data={
            "name": "ado-deploy",
            "provider": "azure_devops",
            "credential_id": "",
            "ado_org": "acme",
            "ado_project": "App",
            "ado_pipeline_id": "42",
            "ado_branch": "refs/heads/main",
            "ado_api_base": "https://dev.azure.com",
            "schema_name__0": "version",
            "schema_type__0": "string",
            "schema_required__0": "1",
            "schema_name__1": "var.deploy_env",
            "schema_type__1": "choice",
            "schema_options__1": "blue, green",
            "schema_default__1": "blue",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    return int(resp.headers["location"].split("/")[-1])


def test_ado_detail_shows_var_prefix_hint(auth_client):
    pid = _make_ado_pipeline(auth_client)
    body = auth_client.get(f"/pipelines/{pid}").text
    assert "templateParameters" in body
    assert "var." in body
    # Both schema rows render - including the dotted name field
    assert 'name="input__version"' in body
    assert 'name="input__var.deploy_env"' in body


def test_ado_run_persists_inputs_with_var_prefix(auth_client, db):
    pid = _make_ado_pipeline(auth_client)
    resp = auth_client.post(
        f"/pipelines/{pid}/run",
        data={
            "input__version": "9.9.9",
            "input__var.deploy_env": "green",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    from dosm.models import PipelineRun
    run = db.query(PipelineRun).filter_by(pipeline_id=pid).one()
    stored = json.loads(run.inputs)
    assert stored == {"version": "9.9.9", "var.deploy_env": "green"}


def test_tfc_run_form_shows_no_inputs_banner(auth_client):
    resp = auth_client.post(
        "/pipelines/new",
        data={
            "name": "tfc-run",
            "provider": "terraform_cloud",
            "credential_id": "",
            "tfc_base_url": "https://app.terraform.io",
            "tfc_workspace_id": "ws-abc123",
            "tfc_auto_apply": "false",
            "tfc_is_destroy": "false",
            "tfc_message": "from DOSM",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    pid = int(resp.headers["location"].split("/")[-1])
    body = auth_client.get(f"/pipelines/{pid}").text
    assert "Terraform Cloud doesn" in body
    assert "workspace variables" in body


def test_legacy_pipeline_without_schema_uses_text_inputs(auth_client, db):
    resp = auth_client.post(
        "/pipelines/new",
        data={
            "name": "deploy-legacy",
            "provider": "github_actions",
            "credential_id": "",
            "gh_owner": "acme",
            "gh_repo": "app",
            "gh_workflow": "deploy.yml",
            "gh_ref": "main",
            "gh_api_base": "https://api.github.com",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    pid = int(resp.headers["location"].split("/")[-1])

    detail = auth_client.get(f"/pipelines/{pid}")
    assert 'name="inputs_text"' in detail.text

    run_resp = auth_client.post(
        f"/pipelines/{pid}/run",
        data={"inputs_text": "version=9.9.9\nflag=true"},
        follow_redirects=False,
    )
    assert run_resp.status_code == 303
    from dosm.models import PipelineRun
    run = db.query(PipelineRun).filter_by(pipeline_id=pid).one()
    assert json.loads(run.inputs) == {"version": "9.9.9", "flag": "true"}
