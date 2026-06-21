from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dosm.auth.deps import require_operator, require_user, user_has_role
from dosm.auth.tenancy import active_tenant_id, require_active_tenant
from dosm.db import get_session
from dosm.hosts import repo as hosts_repo  # for credential helpers reuse
from dosm.models import AuditLog, User
from dosm.pipelines import repo
from dosm.pipelines.adapters import PipelineProviderError, get_adapter, list_providers
from dosm.pipelines.inputs import (
    InputValidationError,
    coerce_run_inputs,
    normalize_schema,
    parse_schema_form,
    validate_payload_values,
)
from dosm.pipelines.payload_access import (
    can_see_payload,
    visible_payloads_filter,
)
from dosm.recording import events as rec_events

router = APIRouter(prefix="/pipelines")


def _templates(request: Request):
    return request.app.state.templates


def _form_context(db: Session, user: User, tid: int | None, pipeline=None, error: str | None = None) -> dict:
    cfg: dict = {}
    schema: list = []
    if pipeline is not None:
        try:
            cfg = json.loads(pipeline.config) if pipeline.config else {}
        except json.JSONDecodeError:
            cfg = {}
        if pipeline.inputs_schema:
            try:
                schema = normalize_schema(json.loads(pipeline.inputs_schema) or [])
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
        "credentials": hosts_repo.list_credentials(db, tid),
        "user": user,
        "error": error,
    }


def _decode_inputs_text(raw: str) -> dict:
    """Free-form fallback for pipelines without a typed schema: lines of
    `key=value`, or a JSON object. Empty -> {}."""
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
    tid: int | None = Depends(active_tenant_id),
):
    pipelines = repo.list_pipelines(db, tid)
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
        enriched.append({"p": p, "latest": latest[0] if latest else None,
                         "cfg": cfg, "summary": summary, "provider_name": provider_name})
    return _templates(request).TemplateResponse(
        request, "pipelines/list.html", {"rows": enriched, "user": user}
    )


@router.get("/new", response_class=HTMLResponse, include_in_schema=False)
async def pipelines_new(
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
    tid: int | None = Depends(active_tenant_id),
):
    return _templates(request).TemplateResponse(
        request, "pipelines/form.html", _form_context(db, user, tid)
    )


@router.post("/new", include_in_schema=False)
async def pipelines_create(
    request: Request,
    name: str = Form(...),
    provider: str = Form("github_actions"),
    description: str = Form(""),
    credential_id: str = Form(""),
    db: Session = Depends(get_session),
    user: User = Depends(require_operator),
    tid: int = Depends(require_active_tenant),
):
    form = await request.form()
    config = _decode_config_form(provider, form)
    schema_rows = parse_schema_form(form) or None
    try:
        p = repo.create_pipeline(
            db,
            tenant_id=tid,
            name=name.strip(),
            provider=provider,
            description=description.strip() or None,
            config={k: v for k, v in config.items() if v not in (None, "")},
            inputs_schema=schema_rows,
            credential_id=_parse_int_or_none(credential_id),
        )
    except (IntegrityError, PipelineProviderError) as e:
        db.rollback()
        return _templates(request).TemplateResponse(
            request,
            "pipelines/form.html",
            _form_context(db, user, tid, error=str(e.__cause__ or e)),
            status_code=400,
        )
    db.add(
        AuditLog(
            tenant_id=tid,
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
    tid: int | None = Depends(active_tenant_id),
):
    p = repo.get_pipeline(db, pid, tid)
    if p is None:
        raise HTTPException(404)
    runs = repo.list_runs(db, p.id, limit=25)
    cfg = json.loads(p.config or "{}")
    schema = normalize_schema(json.loads(p.inputs_schema)) if p.inputs_schema else []
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

    payloads_view, payload_values = _build_payloads_view(db, p, schema, user)
    return _templates(request).TemplateResponse(
        request,
        "pipelines/detail.html",
        {"p": p, "cfg": cfg, "schema": schema, "runs": runs_view, "user": user,
         "target_summary": target_summary, "provider_name": provider_name,
         "payloads": payloads_view, "payload_values_json": json.dumps(payload_values),
         "can_manage_payloads": user_has_role(user, "operator")},
    )


def _build_payloads_view(db: Session, pipeline, schema: list[dict], user: User):
    """Return (list-of-view-dicts, {payload_id: values}) for the payloads the
    user may see. Each view dict carries the payload, its values, and any
    schema-drift errors so the template can flag stale ones."""
    payloads = repo.list_payloads(db, pipeline.id, clause=visible_payloads_filter(user))
    view = []
    values_map: dict[str, dict] = {}
    for pl in payloads:
        values = json.loads(pl.values_json or "{}")
        values_map[str(pl.id)] = values
        stale = validate_payload_values(schema, values) if schema else []
        view.append({"payload": pl, "values": values, "stale": stale})
    return view, values_map


@router.get("/{pid}/edit", response_class=HTMLResponse, include_in_schema=False)
async def pipelines_edit(
    pid: int,
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
    tid: int | None = Depends(active_tenant_id),
):
    p = repo.get_pipeline(db, pid, tid)
    if p is None:
        raise HTTPException(404)
    return _templates(request).TemplateResponse(
        request, "pipelines/form.html", _form_context(db, user, tid, pipeline=p)
    )


@router.post("/{pid}/edit", include_in_schema=False)
async def pipelines_update(
    pid: int,
    request: Request,
    name: str = Form(...),
    provider: str = Form("github_actions"),
    description: str = Form(""),
    credential_id: str = Form(""),
    db: Session = Depends(get_session),
    user: User = Depends(require_operator),
    tid: int | None = Depends(active_tenant_id),
):
    p = repo.get_pipeline(db, pid, tid)
    if p is None:
        raise HTTPException(404)
    form = await request.form()
    config = _decode_config_form(provider, form)
    schema_rows = parse_schema_form(form) or None
    try:
        repo.update_pipeline(
            db,
            p,
            name=name.strip(),
            provider=provider,
            description=description.strip() or None,
            config={k: v for k, v in config.items() if v not in (None, "")},
            inputs_schema=schema_rows,
            credential_id=_parse_int_or_none(credential_id),
        )
    except (IntegrityError, PipelineProviderError) as e:
        db.rollback()
        return _templates(request).TemplateResponse(
            request,
            "pipelines/form.html",
            _form_context(db, user, tid, pipeline=p, error=str(e.__cause__ or e)),
            status_code=400,
        )
    db.add(AuditLog(tenant_id=p.tenant_id, actor_id=user.id, action="pipeline.update", target=f"pipeline:{p.id}"))
    db.commit()
    return RedirectResponse(f"/pipelines/{p.id}", status_code=303)


@router.post("/{pid}/delete", include_in_schema=False)
async def pipelines_delete(
    pid: int,
    db: Session = Depends(get_session),
    user: User = Depends(require_operator),
    tid: int | None = Depends(active_tenant_id),
):
    p = repo.get_pipeline(db, pid, tid)
    if p is None:
        raise HTTPException(404)
    audit_tid = p.tenant_id
    repo.delete_pipeline(db, p)
    db.add(AuditLog(tenant_id=audit_tid, actor_id=user.id, action="pipeline.delete", target=f"pipeline:{pid}"))
    db.commit()
    return RedirectResponse("/pipelines", status_code=303)


@router.post("/{pid}/run", include_in_schema=False)
async def pipelines_trigger(
    pid: int,
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_operator),
    tid: int | None = Depends(active_tenant_id),
):
    p = repo.get_pipeline(db, pid, tid)
    if p is None:
        raise HTTPException(404)
    cfg = request.app.state.config
    form = await request.form()
    schema = normalize_schema(json.loads(p.inputs_schema)) if p.inputs_schema else []
    if schema:
        try:
            inputs = coerce_run_inputs(schema, form)
        except InputValidationError as e:
            return _templates(request).TemplateResponse(
                request,
                "pipelines/detail.html",
                {
                    "p": p,
                    "cfg": json.loads(p.config or "{}"),
                    "schema": schema,
                    "runs": [
                        {"r": r, "inputs": json.loads(r.inputs) if r.inputs else {}}
                        for r in repo.list_runs(db, p.id, limit=25)
                    ],
                    "user": user,
                    "target_summary": get_adapter(p.provider).target_summary(json.loads(p.config or "{}")) if p.provider else "",
                    "provider_name": get_adapter(p.provider).display_name or p.provider,
                    "input_error": str(e),
                    "submitted_inputs": {k: form.get(f"input__{k}") for k in (r["name"] for r in schema)},
                },
                status_code=400,
            )
    else:
        inputs = _decode_inputs_text(form.get("inputs_text") or "")
    # Note which payload (if any) the run was launched from, for traceability.
    # Pre-fill is editable, so this records the starting point, not an exact match.
    payload_note = ""
    payload_id = form.get("payload_id")
    if payload_id:
        pl = repo.get_payload(db, int(payload_id)) if str(payload_id).isdigit() else None
        if pl is not None and pl.pipeline_id == p.id and can_see_payload(user, pl):
            payload_note = f" payload={pl.name!r}"

    run = await repo.trigger_pipeline(cfg, db, p, inputs=inputs, user_id=user.id)
    rec_events.record_pipeline_triggered(user.id, p.name, run.id, p.provider)
    db.add(
        AuditLog(
            tenant_id=p.tenant_id,
            actor_id=user.id,
            action="pipeline.run" if run.status != "failed" else "pipeline.run.fail",
            target=f"pipeline:{p.id}",
            details=(
                f"run={run.id} status={run.status}"
                + payload_note
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
    tid: int | None = Depends(active_tenant_id),
):
    run = repo.get_run(db, run_id)
    if run is None:
        raise HTTPException(404)
    # Scope the run through its (tenant-scoped) pipeline - 404 cross-tenant.
    p = repo.get_pipeline(db, run.pipeline_id, tid)
    if p is None:
        raise HTTPException(404)
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
    user: User = Depends(require_operator),
    tid: int | None = Depends(active_tenant_id),
):
    run = repo.get_run(db, run_id)
    if run is None:
        raise HTTPException(404)
    # Scope the run through its (tenant-scoped) pipeline - 404 cross-tenant.
    p_for_rec = repo.get_pipeline(db, run.pipeline_id, tid)
    if p_for_rec is None:
        raise HTTPException(404)
    cfg = request.app.state.config
    old_status = run.status
    await repo.refresh_run(cfg, db, run)
    if run.status != old_status:
        rec_events.record_pipeline_status(user.id, p_for_rec.name, run.id, run.status)
    db.add(
        AuditLog(
            tenant_id=p_for_rec.tenant_id,
            actor_id=user.id,
            action="pipeline.run.refresh",
            target=f"pipeline_run:{run.id}",
            details=f"status={run.status}",
        )
    )
    return RedirectResponse(f"/pipelines/runs/{run.id}", status_code=303)


# ---- Payloads (saved input sets) -----------------------------------------


def _schema_for(p) -> list[dict]:
    return normalize_schema(json.loads(p.inputs_schema)) if p.inputs_schema else []


def _payload_values_from_form(schema: list[dict], form) -> dict:
    """Build a payload's stored values from a submitted form. Typed pipelines go
    through the same coercion/validation as a real run; schemaless ones keep the
    raw ``inputs_text`` so the textarea can be pre-filled verbatim."""
    if schema:
        return coerce_run_inputs(schema, form)
    raw = (form.get("inputs_text") or "").strip()
    return {"__raw__": raw} if raw else {}


def _payload_or_404(db, pid: int, payload_id: int, user, tid: int | None):
    """Load a payload, enforcing it belongs to the (tenant-scoped) pipeline and
    the user may see it (404 - not 403 - so private payloads don't leak)."""
    p = repo.get_pipeline(db, pid, tid)
    if p is None:
        raise HTTPException(404)
    pl = repo.get_payload(db, payload_id)
    if pl is None or pl.pipeline_id != pid or not can_see_payload(user, pl):
        raise HTTPException(404)
    return p, pl


@router.get("/{pid}/payloads/new", response_class=HTMLResponse, include_in_schema=False)
async def payload_new_form(
    pid: int,
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_operator),
    tid: int | None = Depends(active_tenant_id),
):
    p = repo.get_pipeline(db, pid, tid)
    if p is None:
        raise HTTPException(404)
    return _templates(request).TemplateResponse(
        request,
        "pipelines/payload_form.html",
        {"p": p, "schema": _schema_for(p), "payload": None, "values": {}, "user": user},
    )


@router.post("/{pid}/payloads/new", include_in_schema=False)
async def payload_create(
    pid: int,
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    visibility: str = Form("shared"),
    db: Session = Depends(get_session),
    user: User = Depends(require_operator),
    tid: int | None = Depends(active_tenant_id),
):
    p = repo.get_pipeline(db, pid, tid)
    if p is None:
        raise HTTPException(404)
    schema = _schema_for(p)
    form = await request.form()
    try:
        values = _payload_values_from_form(schema, form)
        pl = repo.create_payload(
            db,
            pipeline_id=pid,
            name=name,
            values=values,
            description=description,
            visibility=visibility,
            created_by_id=user.id,
        )
    except (InputValidationError, repo.PayloadNameConflict, ValueError) as e:
        return _templates(request).TemplateResponse(
            request,
            "pipelines/payload_form.html",
            {"p": p, "schema": schema, "payload": None,
             "values": {k.removeprefix("input__"): v for k, v in form.items() if k.startswith("input__")},
             "user": user, "error": str(e), "name": name, "description": description,
             "visibility": visibility},
            status_code=400,
        )
    db.add(AuditLog(tenant_id=p.tenant_id, actor_id=user.id, action="payload.create",
                    target=f"pipeline:{pid}", details=f"payload={pl.id} name={pl.name!r} visibility={pl.visibility}"))
    db.commit()
    return RedirectResponse(f"/pipelines/{pid}", status_code=303)


@router.get("/{pid}/payloads/{payload_id}/edit", response_class=HTMLResponse, include_in_schema=False)
async def payload_edit_form(
    pid: int,
    payload_id: int,
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_operator),
    tid: int | None = Depends(active_tenant_id),
):
    p, pl = _payload_or_404(db, pid, payload_id, user, tid)
    return _templates(request).TemplateResponse(
        request,
        "pipelines/payload_form.html",
        {"p": p, "schema": _schema_for(p), "payload": pl,
         "values": json.loads(pl.values_json or "{}"), "user": user},
    )


@router.post("/{pid}/payloads/{payload_id}/edit", include_in_schema=False)
async def payload_update(
    pid: int,
    payload_id: int,
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    visibility: str = Form("shared"),
    db: Session = Depends(get_session),
    user: User = Depends(require_operator),
    tid: int | None = Depends(active_tenant_id),
):
    p, pl = _payload_or_404(db, pid, payload_id, user, tid)
    schema = _schema_for(p)
    form = await request.form()
    try:
        values = _payload_values_from_form(schema, form)
        repo.update_payload(db, pl, name=name, values=values,
                            description=description, visibility=visibility)
    except (InputValidationError, repo.PayloadNameConflict, ValueError) as e:
        return _templates(request).TemplateResponse(
            request,
            "pipelines/payload_form.html",
            {"p": p, "schema": schema, "payload": pl,
             "values": {k.removeprefix("input__"): v for k, v in form.items() if k.startswith("input__")},
             "user": user, "error": str(e)},
            status_code=400,
        )
    db.add(AuditLog(tenant_id=p.tenant_id, actor_id=user.id, action="payload.update",
                    target=f"pipeline:{pid}", details=f"payload={pl.id} name={pl.name!r}"))
    db.commit()
    return RedirectResponse(f"/pipelines/{pid}", status_code=303)


@router.post("/{pid}/payloads/{payload_id}/rename", include_in_schema=False)
async def payload_rename(
    pid: int,
    payload_id: int,
    name: str = Form(...),
    db: Session = Depends(get_session),
    user: User = Depends(require_operator),
    tid: int | None = Depends(active_tenant_id),
):
    p, pl = _payload_or_404(db, pid, payload_id, user, tid)
    old = pl.name
    try:
        repo.update_payload(db, pl, name=name)
    except (repo.PayloadNameConflict, ValueError) as e:
        raise HTTPException(400, str(e)) from e
    db.add(AuditLog(tenant_id=p.tenant_id, actor_id=user.id, action="payload.rename",
                    target=f"pipeline:{pid}", details=f"payload={pl.id} {old!r} -> {pl.name!r}"))
    db.commit()
    return RedirectResponse(f"/pipelines/{pid}", status_code=303)


@router.post("/{pid}/payloads/{payload_id}/copy", include_in_schema=False)
async def payload_copy(
    pid: int,
    payload_id: int,
    db: Session = Depends(get_session),
    user: User = Depends(require_operator),
    tid: int | None = Depends(active_tenant_id),
):
    p, pl = _payload_or_404(db, pid, payload_id, user, tid)
    new = repo.copy_payload(db, pl, created_by_id=user.id)
    db.add(AuditLog(tenant_id=p.tenant_id, actor_id=user.id, action="payload.copy",
                    target=f"pipeline:{pid}", details=f"from={pl.id} to={new.id} name={new.name!r}"))
    db.commit()
    return RedirectResponse(f"/pipelines/{pid}", status_code=303)


@router.post("/{pid}/payloads/{payload_id}/delete", include_in_schema=False)
async def payload_delete(
    pid: int,
    payload_id: int,
    db: Session = Depends(get_session),
    user: User = Depends(require_operator),
    tid: int | None = Depends(active_tenant_id),
):
    p, pl = _payload_or_404(db, pid, payload_id, user, tid)
    name = pl.name
    audit_tid = p.tenant_id
    repo.delete_payload(db, pl)
    db.add(AuditLog(tenant_id=audit_tid, actor_id=user.id, action="payload.delete",
                    target=f"pipeline:{pid}", details=f"payload={payload_id} name={name!r}"))
    db.commit()
    return RedirectResponse(f"/pipelines/{pid}", status_code=303)
