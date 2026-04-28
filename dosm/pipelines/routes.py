from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dosm.auth.deps import require_user
from dosm.db import get_session
from dosm.hosts import repo as hosts_repo  # for credential helpers reuse
from dosm.models import AuditLog, User
from dosm.pipelines import repo
from dosm.pipelines.adapters import PipelineProviderError, get_adapter, list_providers
from dosm.recording import events as rec_events

router = APIRouter(prefix="/pipelines")


def _templates(request: Request):
    return request.app.state.templates


def _form_context(db: Session, user: User, pipeline=None, error: str | None = None) -> dict:
    cfg: dict = {}
    schema: list = []
    if pipeline is not None:
        try:
            cfg = json.loads(pipeline.config) if pipeline.config else {}
        except json.JSONDecodeError:
            cfg = {}
        if pipeline.inputs_schema:
            try:
                schema = json.loads(pipeline.inputs_schema) or []
            except json.JSONDecodeError:
                schema = []
    providers = list_providers()
    field_schemas = {p: get_adapter(p).field_schema() for p in providers}
    cred_hints = {p: get_adapter(p).credential_hint for p in providers}
    provider_names = {p: get_adapter(p).display_name or p for p in providers}
    selected = pipeline.provider if pipeline else (providers[0] if providers else "")
    return {
        "pipeline": pipeline,
        "cfg_parsed": cfg,
        "schema_parsed": schema,
        "providers": providers,
        "provider_names": provider_names,
        "field_schemas": field_schemas,
        "cred_hints": cred_hints,
        "selected_provider": selected,
        "credentials": hosts_repo.list_credentials(db),
        "user": user,
        "error": error,
    }


def _decode_inputs(raw: str) -> dict:
    """Form value: lines of `key=value`, or a JSON object. Empty -> {}."""
    s = (raw or "").strip()
    if not s:
        return {}
    if s.startswith("{"):
        try:
            obj = json.loads(s)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            return {}
    out: dict = {}
    for line in s.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _decode_config_form(provider: str, form: dict) -> dict:
    """Pull provider-specific config fields out of the form using field_schema."""
    try:
        adapter = get_adapter(provider)
    except PipelineProviderError:
        return {}
    return {
        f.config_key: (form.get(f.name) or "").strip() or None
        for f in adapter.field_schema()
    }


def _parse_int_or_none(v: str) -> int | None:
    return int(v) if (v or "").strip() else None


@router.get("", response_class=HTMLResponse, include_in_schema=False)
async def pipelines_list(
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    pipelines = repo.list_pipelines(db)
    enriched = []
    for p in pipelines:
        latest = repo.list_runs(db, p.id, limit=1)
        cfg = json.loads(p.config or "{}")
        try:
            adapter = get_adapter(p.provider)
            summary = adapter.target_summary(cfg)
            provider_name = adapter.display_name or p.provider
        except Exception:
            summary = ""
            provider_name = p.provider
        enriched.append({"p": p, "latest": latest[0] if latest else None, "cfg": cfg, "summary": summary, "provider_name": provider_name})
    return _templates(request).TemplateResponse(
        request, "pipelines/list.html", {"rows": enriched, "user": user}
    )


@router.get("/new", response_class=HTMLResponse, include_in_schema=False)
async def pipelines_new(
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    return _templates(request).TemplateResponse(
        request, "pipelines/form.html", _form_context(db, user)
    )


@router.post("/new", include_in_schema=False)
async def pipelines_create(
    request: Request,
    name: str = Form(...),
    provider: str = Form("github_actions"),
    description: str = Form(""),
    credential_id: str = Form(""),
    inputs_schema: str = Form(""),
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    form = await request.form()
    config = _decode_config_form(provider, form)
    schema_lines = [
        {"name": ln.strip()}
        for ln in (inputs_schema or "").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    try:
        p = repo.create_pipeline(
            db,
            name=name.strip(),
            provider=provider,
            description=description.strip() or None,
            config={k: v for k, v in config.items() if v not in (None, "")},
            inputs_schema=schema_lines or None,
            credential_id=_parse_int_or_none(credential_id),
        )
    except (IntegrityError, PipelineProviderError) as e:
        db.rollback()
        return _templates(request).TemplateResponse(
            request,
            "pipelines/form.html",
            _form_context(db, user, error=str(e.__cause__ or e)),
            status_code=400,
        )
    db.add(
        AuditLog(
            actor_id=user.id,
            action="pipeline.create",
            target=f"pipeline:{p.id}",
            details=f"provider={provider}",
        )
    )
    return RedirectResponse(f"/pipelines/{p.id}", status_code=303)


@router.get("/{pid}", response_class=HTMLResponse, include_in_schema=False)
async def pipelines_detail(
    pid: int,
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    p = repo.get_pipeline(db, pid)
    if p is None:
        raise HTTPException(404)
    runs = repo.list_runs(db, p.id, limit=25)
    cfg = json.loads(p.config or "{}")
    schema = json.loads(p.inputs_schema) if p.inputs_schema else []
    runs_view = [
        {"r": r, "inputs": json.loads(r.inputs) if r.inputs else {}} for r in runs
    ]
    try:
        adapter = get_adapter(p.provider)
        target_summary = adapter.target_summary(cfg)
        provider_name = adapter.display_name or p.provider
    except Exception:
        target_summary = ""
        provider_name = p.provider
    return _templates(request).TemplateResponse(
        request,
        "pipelines/detail.html",
        {"p": p, "cfg": cfg, "schema": schema, "runs": runs_view, "user": user,
         "target_summary": target_summary, "provider_name": provider_name},
    )


@router.get("/{pid}/edit", response_class=HTMLResponse, include_in_schema=False)
async def pipelines_edit(
    pid: int,
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    p = repo.get_pipeline(db, pid)
    if p is None:
        raise HTTPException(404)
    return _templates(request).TemplateResponse(
        request, "pipelines/form.html", _form_context(db, user, pipeline=p)
    )


@router.post("/{pid}/edit", include_in_schema=False)
async def pipelines_update(
    pid: int,
    request: Request,
    name: str = Form(...),
    provider: str = Form("github_actions"),
    description: str = Form(""),
    credential_id: str = Form(""),
    inputs_schema: str = Form(""),
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    p = repo.get_pipeline(db, pid)
    if p is None:
        raise HTTPException(404)
    form = await request.form()
    config = _decode_config_form(provider, form)
    schema_lines = [
        {"name": ln.strip()}
        for ln in (inputs_schema or "").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    try:
        repo.update_pipeline(
            db,
            p,
            name=name.strip(),
            provider=provider,
            description=description.strip() or None,
            config={k: v for k, v in config.items() if v not in (None, "")},
            inputs_schema=schema_lines or None,
            credential_id=_parse_int_or_none(credential_id),
        )
    except (IntegrityError, PipelineProviderError) as e:
        db.rollback()
        return _templates(request).TemplateResponse(
            request,
            "pipelines/form.html",
            _form_context(db, user, pipeline=p, error=str(e.__cause__ or e)),
            status_code=400,
        )
    db.add(AuditLog(actor_id=user.id, action="pipeline.update", target=f"pipeline:{p.id}"))
    db.commit()
    return RedirectResponse(f"/pipelines/{p.id}", status_code=303)


@router.post("/{pid}/delete", include_in_schema=False)
async def pipelines_delete(
    pid: int,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    p = repo.get_pipeline(db, pid)
    if p is None:
        raise HTTPException(404)
    repo.delete_pipeline(db, p)
    db.add(AuditLog(actor_id=user.id, action="pipeline.delete", target=f"pipeline:{pid}"))
    db.commit()
    return RedirectResponse("/pipelines", status_code=303)


@router.post("/{pid}/run", include_in_schema=False)
async def pipelines_trigger(
    pid: int,
    request: Request,
    inputs_text: str = Form(""),
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    p = repo.get_pipeline(db, pid)
    if p is None:
        raise HTTPException(404)
    cfg = request.app.state.config
    inputs = _decode_inputs(inputs_text)
    run = await repo.trigger_pipeline(cfg, db, p, inputs=inputs, user_id=user.id)
    rec_events.record_pipeline_triggered(user.id, p.name, run.id, p.provider)
    db.add(
        AuditLog(
            actor_id=user.id,
            action="pipeline.run" if run.status != "failed" else "pipeline.run.fail",
            target=f"pipeline:{p.id}",
            details=(
                f"run={run.id} status={run.status}"
                + (f" external={run.external_id}" if run.external_id else "")
                + (f" error={(run.error or '')[:120]}" if run.error else "")
            ),
        )
    )
    return RedirectResponse(f"/pipelines/runs/{run.id}", status_code=303)


@router.get("/runs/{run_id}", response_class=HTMLResponse, include_in_schema=False)
async def pipelines_run_detail(
    run_id: int,
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    run = repo.get_run(db, run_id)
    if run is None:
        raise HTTPException(404)
    p = repo.get_pipeline(db, run.pipeline_id)
    inputs = json.loads(run.inputs) if run.inputs else {}
    return _templates(request).TemplateResponse(
        request,
        "pipelines/run_detail.html",
        {"run": run, "pipeline": p, "inputs": inputs, "user": user},
    )


@router.post("/runs/{run_id}/refresh", include_in_schema=False)
async def pipelines_run_refresh(
    run_id: int,
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    run = repo.get_run(db, run_id)
    if run is None:
        raise HTTPException(404)
    cfg = request.app.state.config
    old_status = run.status
    await repo.refresh_run(cfg, db, run)
    p_for_rec = repo.get_pipeline(db, run.pipeline_id)
    if p_for_rec and run.status != old_status:
        rec_events.record_pipeline_status(user.id, p_for_rec.name, run.id, run.status)
    db.add(
        AuditLog(
            actor_id=user.id,
            action="pipeline.run.refresh",
            target=f"pipeline_run:{run.id}",
            details=f"status={run.status}",
        )
    )
    return RedirectResponse(f"/pipelines/runs/{run.id}", status_code=303)
