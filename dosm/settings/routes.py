from __future__ import annotations

import asyncio
import csv
import io
import json
from functools import partial

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from dosm.auth import rbac as rbac_store
from dosm.auth.deps import (
    ROLE_RANK,
    is_platform_admin,
    require_admin,
    require_platform_admin,
)
from dosm.auth.tenancy import active_tenant_id
from dosm.config import update_config_yaml
from dosm.db import get_session
from dosm.models import AuditLog, Tenant, User
from dosm.settings.cli_catalog import detect_all

router = APIRouter(prefix="/settings")

# Valid *tenant* DOSM roles for the RBAC mapping editor (a group grant never
# assigns platform_admin - that role is tenant-less and set explicitly).
ROLES = tuple(r for r in sorted(ROLE_RANK, key=lambda r: ROLE_RANK[r]) if r != "platform_admin")


def _resolve_mapping_tenant(db: Session, user: User, tenant_id_form: str | None) -> int:
    """Tenant a group-mapping edit targets. A tenant admin can only touch their
    own tenant; a platform admin must name the tenant via the form."""
    if is_platform_admin(user):
        if not tenant_id_form:
            raise HTTPException(400, "select a tenant for this mapping")
        tid = int(tenant_id_form)
        if db.get(Tenant, tid) is None:
            raise HTTPException(404, "no such tenant")
        return tid
    if user.tenant_id is None:
        raise HTTPException(403, "your account is not assigned to a tenant")
    return user.tenant_id


def _templates(request: Request):
    return request.app.state.templates


def _save_rbac(cfg, *, group_role_map=None, default_role=None) -> None:
    """Persist the rbac block to config.yaml and reflect it on the live cfg so
    the change takes effect without a process restart."""
    grm = dict(cfg.rbac.group_role_map) if group_role_map is None else group_role_map
    dr = cfg.rbac.default_role if default_role is None else default_role
    update_config_yaml(cfg.home, {"rbac": {"group_role_map": grm, "default_role": dr}})
    cfg.rbac.group_role_map = grm
    cfg.rbac.default_role = dr


@router.get("", response_class=HTMLResponse, include_in_schema=False)
async def settings_home(
    request: Request,
    user: User = Depends(require_admin),
):
    cfg = request.app.state.config
    detected = await asyncio.get_event_loop().run_in_executor(
        None, partial(detect_all, with_version=True)
    )
    enabled = cfg.cli_tools or {}
    rows = [
        {
            "spec": d.spec,
            "installed": d.installed,
            "path": d.path,
            "version": d.version,
            "enabled": bool(enabled.get(d.spec.id, False)),
        }
        for d in detected
    ]
    return _templates(request).TemplateResponse(
        request,
        "settings/cli_tools.html",
        {"rows": rows, "user": user},
    )


@router.post("", include_in_schema=False)
async def settings_save(
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_admin),
):
    """Persist the cli_tools toggle map to config.yaml.

    The form submits an `enabled` checkbox per tool id; only checked ids are
    in the form-data, so we materialize the full map from the catalog and
    set False for anything not in the submitted form.
    """
    form = await request.form()
    submitted = set(form.getlist("enabled"))
    detected = await asyncio.get_event_loop().run_in_executor(
        None, partial(detect_all, with_version=False)
    )
    new_map = {d.spec.id: (d.spec.id in submitted) for d in detected}

    cfg = request.app.state.config
    update_config_yaml(cfg.home, {"cli_tools": new_map})
    # Reflect the change on the live request handler's cfg object so the
    # next page render sees fresh state without a process restart.
    cfg.cli_tools = new_map

    enabled_count = sum(1 for v in new_map.values() if v)
    db.add(
        AuditLog(
            actor_id=user.id,
            action="settings.cli_tools.update",
            target="settings",
            details=f"enabled={enabled_count} total={len(new_map)}",
        )
    )
    return RedirectResponse("/settings?saved=1", status_code=303)


@router.get("/integrations", response_class=HTMLResponse, include_in_schema=False)
async def integrations_page(
    request: Request,
    user: User = Depends(require_admin),
):
    cfg = request.app.state.config
    return _templates(request).TemplateResponse(
        request,
        "settings/integrations.html",
        {"user": user, "cfg": cfg},
    )


@router.post("/integrations", include_in_schema=False)
async def integrations_save(
    request: Request,
    guacamole_enabled: str | None = Form(None),
    guacamole_base_url: str = Form(""),
    db: Session = Depends(get_session),
    user: User = Depends(require_admin),
):
    cfg = request.app.state.config
    enabled = guacamole_enabled is not None
    base_url = guacamole_base_url.strip() or cfg.guacamole.base_url

    update_config_yaml(cfg.home, {
        "guacamole": {
            **cfg.guacamole.model_dump(),
            "enabled": enabled,
            "base_url": base_url,
        }
    })
    cfg.guacamole.enabled = enabled
    cfg.guacamole.base_url = base_url

    db.add(AuditLog(
        actor_id=user.id,
        action="settings.integrations.update",
        target="guacamole",
        details=f"enabled={enabled} base_url={base_url}",
    ))
    db.commit()
    return RedirectResponse("/settings/integrations?saved=1", status_code=303)


# ── Access control (RBAC) - AD/Okta group → DOSM role mapping ──────────────


def _tenant_names(db: Session) -> dict[int, str]:
    return {t.id: t.name for t in db.execute(select(Tenant)).scalars()}


def _rbac_rows(db: Session, tid: int | None) -> list[dict]:
    names = _tenant_names(db)
    return [
        {"group": m.group_name, "role": m.role, "tenant_id": m.tenant_id,
         "tenant": names.get(m.tenant_id, "?")}
        for m in rbac_store.list_mappings(db, tid)
    ]


@router.get("/rbac", response_class=HTMLResponse, include_in_schema=False)
async def rbac_page(
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_admin),
    tid: int | None = Depends(active_tenant_id),
):
    cfg = request.app.state.config
    platform = is_platform_admin(user)
    rows = _rbac_rows(db, tid)
    tenants = [{"id": t.id, "name": t.name}
               for t in db.execute(select(Tenant).order_by(Tenant.name)).scalars()]
    return _templates(request).TemplateResponse(
        request,
        "settings/rbac.html",
        {"user": user, "rows": rows, "default_role": cfg.rbac.default_role,
         "roles": list(ROLES), "okta_enabled": cfg.okta.enabled,
         "is_platform_admin": platform, "tenants": tenants},
    )


@router.post("/rbac/mapping", include_in_schema=False)
async def rbac_mapping_save(
    request: Request,
    group: str = Form(...),
    role: str = Form(...),
    tenant_id: str | None = Form(None),
    db: Session = Depends(get_session),
    user: User = Depends(require_admin),
):
    """Upsert one group -> role assignment within a tenant (add a new group or
    change an existing group's role - keyed on group name + tenant)."""
    group = group.strip()
    if not group:
        raise HTTPException(400, "group name is required")
    if role not in ROLES:
        raise HTTPException(400, f"invalid role {role!r}")
    tid = _resolve_mapping_tenant(db, user, tenant_id)
    updated = rbac_store.upsert_mapping(db, group, tid, role)
    db.add(AuditLog(
        tenant_id=tid,
        actor_id=user.id,
        action="settings.rbac.mapping.update" if updated else "settings.rbac.mapping.add",
        target="rbac",
        details=f"group={group!r} role={role} tenant={tid}",
    ))
    db.commit()
    return RedirectResponse("/settings/rbac?saved=1", status_code=303)


@router.post("/rbac/mapping/delete", include_in_schema=False)
async def rbac_mapping_delete(
    request: Request,
    group: str = Form(...),
    tenant_id: str | None = Form(None),
    db: Session = Depends(get_session),
    user: User = Depends(require_admin),
):
    tid = _resolve_mapping_tenant(db, user, tenant_id)
    if not rbac_store.delete_mapping(db, group, tid):
        raise HTTPException(404, "no such group mapping")
    db.add(AuditLog(tenant_id=tid, actor_id=user.id, action="settings.rbac.mapping.delete",
                    target="rbac", details=f"group={group!r} tenant={tid}"))
    db.commit()
    return RedirectResponse("/settings/rbac?saved=1", status_code=303)


@router.post("/rbac/default", include_in_schema=False)
async def rbac_default_save(
    request: Request,
    default_role: str = Form(...),
    db: Session = Depends(get_session),
    user: User = Depends(require_platform_admin),
):
    """The unmapped-user fallback is a *global* policy (it grants into the
    Default tenant), so only a platform admin may change it."""
    cfg = request.app.state.config
    # "none" denies access to users in no mapped group (group membership required).
    if default_role not in ROLE_RANK and default_role != "none":
        raise HTTPException(400, f"invalid role {default_role!r}")
    _save_rbac(cfg, default_role=default_role)
    db.add(AuditLog(actor_id=user.id, action="settings.rbac.default.update",
                    target="rbac", details=f"default_role={default_role}"))
    db.commit()
    return RedirectResponse("/settings/rbac?saved=1", status_code=303)


@router.get("/rbac/export.json", include_in_schema=False)
async def rbac_export_json(
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_admin),
    tid: int | None = Depends(active_tenant_id),
):
    cfg = request.app.state.config
    payload = {"default_role": cfg.rbac.default_role, "groups": _rbac_rows(db, tid)}
    body = json.dumps(payload, indent=2)
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="dosm-rbac-groups.json"'},
    )


@router.get("/rbac/export.csv", include_in_schema=False)
async def rbac_export_csv(
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_admin),
    tid: int | None = Depends(active_tenant_id),
):
    cfg = request.app.state.config
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["group", "tenant", "role"])
    for row in _rbac_rows(db, tid):
        writer.writerow([row["group"], row["tenant"], row["role"]])
    # Trailing row documents the fallback role for groups not listed above.
    writer.writerow(["(default - unmapped groups)", "Default", cfg.rbac.default_role])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="dosm-rbac-groups.csv"'},
    )
