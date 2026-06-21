from __future__ import annotations

import asyncio
import csv
import io
import json
import re
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
from dosm.auth.tenancy import ACTIVE_TENANT_SESSION_KEY, active_tenant_id
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


# -- Tenants (platform admin) + active-tenant switcher ----------------------


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return s or "tenant"


@router.post("/active-tenant", include_in_schema=False)
async def set_active_tenant(
    request: Request,
    tenant_id: str = Form(...),
    db: Session = Depends(get_session),
    user: User = Depends(require_platform_admin),
):
    """Platform admin switches the active tenant they operate within. ``all``
    clears it (the read-only all-tenants overview)."""
    if tenant_id == "all" or not tenant_id:
        request.session.pop(ACTIVE_TENANT_SESSION_KEY, None)
    else:
        tid = int(tenant_id)
        if db.get(Tenant, tid) is None:
            raise HTTPException(404, "no such tenant")
        request.session[ACTIVE_TENANT_SESSION_KEY] = tid
    # Return to wherever the switch was made.
    back = request.headers.get("referer") or "/"
    return RedirectResponse(back, status_code=303)


@router.get("/tenants", response_class=HTMLResponse, include_in_schema=False)
async def tenants_page(
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_platform_admin),
):
    tenants = list(db.execute(select(Tenant).order_by(Tenant.name)).scalars())
    # Member + host counts per tenant for the overview.
    from dosm.models import Host
    counts = {}
    for t in tenants:
        users = db.execute(
            select(User).where(User.tenant_id == t.id)
        ).scalars().all()
        n_hosts = db.execute(
            select(Host.id).where(Host.tenant_id == t.id)
        ).scalars().all()
        counts[t.id] = {"users": len(users), "hosts": len(n_hosts)}
    return _templates(request).TemplateResponse(
        request, "settings/tenants.html",
        {"user": user, "tenants": tenants, "counts": counts},
    )


@router.post("/tenants", include_in_schema=False)
async def tenant_create(
    request: Request,
    name: str = Form(...),
    slug: str = Form(""),
    description: str = Form(""),
    db: Session = Depends(get_session),
    user: User = Depends(require_platform_admin),
):
    name = name.strip()
    if not name:
        raise HTTPException(400, "name is required")
    slug = _slugify(slug or name)
    if db.execute(select(Tenant).where(Tenant.slug == slug)).scalar_one_or_none() is not None:
        raise HTTPException(400, f"a tenant with slug {slug!r} already exists")
    tenant = Tenant(name=name, slug=slug, description=description.strip() or None)
    db.add(tenant)
    db.flush()
    db.add(AuditLog(tenant_id=tenant.id, actor_id=user.id, action="tenant.create",
                    target=f"tenant:{tenant.id}", details=f"name={name!r} slug={slug}"))
    db.commit()
    return RedirectResponse("/settings/tenants?saved=1", status_code=303)


@router.post("/tenants/{tenant_id}/rename", include_in_schema=False)
async def tenant_rename(
    tenant_id: int,
    request: Request,
    name: str = Form(...),
    db: Session = Depends(get_session),
    user: User = Depends(require_platform_admin),
):
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(404)
    old = tenant.name
    tenant.name = name.strip() or tenant.name
    db.add(AuditLog(tenant_id=tenant.id, actor_id=user.id, action="tenant.update",
                    target=f"tenant:{tenant.id}", details=f"name {old!r} -> {tenant.name!r}"))
    db.commit()
    return RedirectResponse("/settings/tenants?saved=1", status_code=303)


@router.post("/tenants/{tenant_id}/toggle", include_in_schema=False)
async def tenant_toggle(
    tenant_id: int,
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_platform_admin),
):
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(404)
    tenant.is_active = not tenant.is_active
    db.add(AuditLog(tenant_id=tenant.id, actor_id=user.id, action="tenant.update",
                    target=f"tenant:{tenant.id}", details=f"is_active={tenant.is_active}"))
    db.commit()
    return RedirectResponse("/settings/tenants?saved=1", status_code=303)


# -- Members (per-tenant users + per-user role pin/lock) --------------------


@router.get("/members", response_class=HTMLResponse, include_in_schema=False)
async def members_page(
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_admin),
    tid: int | None = Depends(active_tenant_id),
):
    """List users a tenant admin (their tenant) or platform admin (all) can
    manage, with inline role + lock controls. The per-user override surfaces
    here: pinning a role stops Okta group claims from changing it."""
    platform = is_platform_admin(user)
    stmt = select(User).order_by(User.username)
    if not platform:
        stmt = stmt.where(User.tenant_id == tid)
    users = list(db.execute(stmt).scalars())
    tenant_names = _tenant_names(db)
    tenants = [{"id": t.id, "name": t.name}
               for t in db.execute(select(Tenant).order_by(Tenant.name)).scalars()]
    return _templates(request).TemplateResponse(
        request, "settings/members.html",
        {"user": user, "members": users, "roles": list(ROLES),
         "is_platform_admin": platform, "tenant_names": tenant_names,
         "tenants": tenants},
    )


@router.post("/members/{user_id}/role", include_in_schema=False)
async def member_set_role(
    user_id: int,
    request: Request,
    role: str = Form(...),
    locked: str | None = Form(None),
    tenant_id: str | None = Form(None),
    db: Session = Depends(get_session),
    user: User = Depends(require_admin),
    tid: int | None = Depends(active_tenant_id),
):
    """Set a user's role + lock (and tenant, platform admin only). A tenant
    admin may only manage users in their own tenant and may not grant
    platform_admin."""
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(404)
    platform = is_platform_admin(user)
    if not platform:
        # Tenant admin: confined to their own tenant + tenant roles only.
        if tid is None or target.tenant_id != tid:
            raise HTTPException(404)
        if role not in ROLES:
            raise HTTPException(400, f"invalid role {role!r}")
    else:
        # Platform admin: may also grant platform_admin and reassign tenant.
        if role not in ROLE_RANK:
            raise HTTPException(400, f"invalid role {role!r}")
        if role == "platform_admin":
            target.tenant_id = None
        elif tenant_id:
            new_tid = int(tenant_id)
            if db.get(Tenant, new_tid) is None:
                raise HTTPException(404, "no such tenant")
            target.tenant_id = new_tid
    old = target.role
    target.role = role
    target.role_locked = locked is not None
    db.add(AuditLog(
        tenant_id=target.tenant_id, actor_id=user.id, action="user.set_role",
        target=f"user:{target.id}",
        details=f"{old} -> {role} locked={target.role_locked}",
    ))
    db.commit()
    return RedirectResponse("/settings/members?saved=1", status_code=303)
