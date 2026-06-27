from __future__ import annotations

import asyncio
import csv
import io
import json
import re
from datetime import UTC, datetime, timedelta
from functools import partial
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import func, select
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

# Tenant-confined DOSM roles, low→high (viewer, operator, tenant_admin). These
# are the roles a tenant admin can assign in Members; platform_admin is excluded
# (it's tenant-less and only a platform admin may grant it).
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
    """Group → tenant grants visible to the caller. Every mapping grants the
    baseline ``viewer`` role within its tenant (elevation happens in Members),
    so there is no per-row role. Tenant-less rows no longer exist (the
    platform_admin-via-group grant was retired) but are skipped defensively."""
    names = _tenant_names(db)
    return [
        {"id": m.id, "group": m.group_name, "tenant_id": m.tenant_id,
         "tenant": names.get(m.tenant_id, "?")}
        for m in rbac_store.list_mappings(db, tid)
        if m.tenant_id is not None
    ]


def _can_edit_mapping(user: User, mapping) -> bool:
    """Platform admins may edit any mapping; tenant admins only their own
    tenant's tenant-scoped grants (never the tenant-less platform_admin ones)."""
    if is_platform_admin(user):
        return True
    return mapping.tenant_id is not None and mapping.tenant_id == user.tenant_id


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
         "okta_enabled": cfg.okta.enabled,
         "is_platform_admin": platform, "tenants": tenants},
    )


@router.post("/rbac/mapping", include_in_schema=False)
async def rbac_mapping_save(
    request: Request,
    group: str = Form(...),
    tenant_id: str | None = Form(None),
    db: Session = Depends(get_session),
    user: User = Depends(require_admin),
):
    """Add (or upsert) a group -> tenant grant. Membership grants the baseline
    ``viewer`` role within that tenant; individuals are elevated in Members. A
    tenant admin can only map groups into their own tenant; a platform admin
    names the tenant in the form."""
    group = group.strip()
    if not group:
        raise HTTPException(400, "group name is required")
    tid = _resolve_mapping_tenant(db, user, tenant_id)
    updated = rbac_store.upsert_mapping(db, group, tid, "viewer")
    db.add(AuditLog(
        tenant_id=tid,
        actor_id=user.id,
        action="settings.rbac.mapping.update" if updated else "settings.rbac.mapping.add",
        target="rbac",
        details=f"group={group!r} tenant={tid} (viewer)",
    ))
    db.commit()
    return RedirectResponse("/settings/rbac?saved=1", status_code=303)


@router.post("/rbac/mapping/delete", include_in_schema=False)
async def rbac_mapping_delete(
    request: Request,
    mapping_id: int = Form(...),
    db: Session = Depends(get_session),
    user: User = Depends(require_admin),
):
    mapping = rbac_store.get_by_id(db, mapping_id)
    if mapping is None or not _can_edit_mapping(user, mapping):
        raise HTTPException(404, "no such group mapping")
    grp, mtid = mapping.group_name, mapping.tenant_id
    rbac_store.delete_by_id(db, mapping)
    db.add(AuditLog(tenant_id=mtid, actor_id=user.id, action="settings.rbac.mapping.delete",
                    target="rbac", details=f"group={grp!r} tenant={mtid}"))
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
    Default tenant), so only a platform admin may change it. Like a group grant,
    it can only confer the baseline ``viewer`` role - or deny access outright."""
    cfg = request.app.state.config
    # "none" denies access to users in no mapped group (group membership required);
    # "viewer" admits everyone who authenticates at the baseline role.
    if default_role not in ("none", "viewer"):
        raise HTTPException(400, "default role must be 'none' or 'viewer'")
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
    # Every mapped group grants the baseline viewer role within its tenant.
    writer.writerow(["group", "tenant", "grants"])
    for row in _rbac_rows(db, tid):
        writer.writerow([row["group"], row["tenant"], "viewer"])
    # Trailing row documents the fallback for users in no mapped group.
    writer.writerow(["(default - unmapped users)", "Default", cfg.rbac.default_role])
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
    description: str = Form(""),
    db: Session = Depends(get_session),
    user: User = Depends(require_platform_admin),
):
    name = name.strip()
    if not name:
        raise HTTPException(400, "name is required")
    # Slug is always derived from the name (stable machine identifier).
    slug = _slugify(name)
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
    tenant_id: str | None = Form(None),
    db: Session = Depends(get_session),
    user: User = Depends(require_admin),
    tid: int | None = Depends(active_tenant_id),
):
    """Set a user's role (and tenant, platform admin only). A tenant admin may
    only manage users in their own tenant and may not grant platform_admin.

    Setting a role here pins it (``role_locked``) so a subsequent Okta sign-in
    won't overwrite the manual assignment from the user's group claims. Use
    ``dosm user set-role <name> <role> --unlock`` to hand control back to the
    group mapping."""
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
        # A tenant-scoped role must end up with a tenant, or the user would be
        # locked out (and could otherwise fall through tenant scoping).
        if role != "platform_admin" and target.tenant_id is None:
            raise HTTPException(400, "assign a tenant for a non-platform role")
    old = target.role
    target.role = role
    # A manual assignment pins the role so Okta group sync won't revert it.
    target.role_locked = True
    db.add(AuditLog(
        tenant_id=target.tenant_id, actor_id=user.id, action="user.set_role",
        target=f"user:{target.id}",
        details=f"{old} -> {role} locked=True",
    ))
    db.commit()
    return RedirectResponse("/settings/members?saved=1", status_code=303)


# -- Audit log (read-only) --------------------------------------------------

AUDIT_PAGE_SIZE = 50


def _parse_day(value: str | None) -> datetime | None:
    """Parse a ``YYYY-MM-DD`` filter value into UTC midnight; ignore junk."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        return None


def _admin_tenant_ids(db: Session, user: User) -> set[int] | None:
    """Tenant ids whose audit log this admin may read. ``None`` means *all*
    tenants (platform admin). A tenant admin gets exactly their own tenant;
    anyone else gets the empty set.

    Admin-of-several-tenants is not representable in the model: a ``User`` has
    a single ``tenant_id`` and only ``platform_admin`` spans tenants. Holding
    tenant_admin in one tenant must never expose another tenant's logs."""
    if is_platform_admin(user):
        return None
    if user.tenant_id is None:
        return set()
    return {user.tenant_id}


def _resolve_audit_request(
    db: Session, user: User, *, action, actor_id, tenant_id, start, end,
):
    """Parse filters and resolve tenant access for both the page and the
    exporter. Returns ``(apply_fn, scope_tids, allowed, actor_pick)`` where
    ``apply_fn`` adds every WHERE clause to a statement, ``scope_tids`` is the
    effective tenant set (``None`` = all), and ``allowed`` is what the caller
    may pick from. Raises 403 if the caller asks for a tenant they don't admin."""
    allowed = _admin_tenant_ids(db, user)
    if allowed is not None and not allowed:
        raise HTTPException(403, "your account is not assigned to a tenant")

    req_tid: int | None = None
    if tenant_id:
        try:
            req_tid = int(tenant_id)
        except ValueError:
            req_tid = None
    # Hard guard: a tenant admin can never reach beyond the tenants they admin.
    if req_tid is not None and allowed is not None and req_tid not in allowed:
        raise HTTPException(403, "you do not administer that tenant")

    scope_tids = {req_tid} if req_tid is not None else allowed

    try:
        actor_pick = int(actor_id) if actor_id else None
    except ValueError:
        actor_pick = None
    start_dt = _parse_day(start)
    end_dt = _parse_day(end)

    def _apply(stmt):
        if scope_tids is not None:
            stmt = stmt.where(AuditLog.tenant_id.in_(scope_tids))
        if action:
            stmt = stmt.where(AuditLog.action == action)
        if actor_pick is not None:
            stmt = stmt.where(AuditLog.actor_id == actor_pick)
        if start_dt is not None:
            stmt = stmt.where(AuditLog.ts >= start_dt)
        if end_dt is not None:
            stmt = stmt.where(AuditLog.ts < end_dt + timedelta(days=1))  # inclusive day
        return stmt

    return _apply, scope_tids, allowed, actor_pick


def _audit_records(db: Session, rows: list[AuditLog]) -> list[dict]:
    """Flatten audit rows to plain dicts (names resolved) for CSV/JSON export."""
    user_names = {u.id: (u.display_name or u.username)
                  for u in db.execute(select(User)).scalars()}
    tenant_names = _tenant_names(db)
    return [{
        "ts": r.ts.isoformat(),
        "action": r.action,
        "actor_id": r.actor_id,
        "actor": user_names.get(r.actor_id),
        "tenant_id": r.tenant_id,
        "tenant": tenant_names.get(r.tenant_id),
        "target": r.target,
        "details": r.details,
        "ip": r.ip,
    } for r in rows]


@router.get("/audit", response_class=HTMLResponse, include_in_schema=False)
async def audit_page(
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_admin),
    action: str | None = None,
    actor_id: str | None = None,
    tenant_id: str | None = None,
    start: str | None = None,
    end: str | None = None,
    page: int = 1,
):
    """Audit-log viewer. A tenant admin sees only their own tenant's events; a
    platform admin sees every tenant and may narrow to one via the filter.
    Filterable by user, tenant, action, and date range; newest first."""
    apply_fn, scope_tids, allowed, actor_pick = _resolve_audit_request(
        db, user, action=action, actor_id=actor_id, tenant_id=tenant_id,
        start=start, end=end)

    total = db.execute(apply_fn(select(func.count()).select_from(AuditLog))).scalar() or 0
    pages = max(1, (total + AUDIT_PAGE_SIZE - 1) // AUDIT_PAGE_SIZE)
    page = max(1, min(page, pages))
    rows = list(db.execute(
        apply_fn(select(AuditLog))
        .order_by(AuditLog.ts.desc())
        .limit(AUDIT_PAGE_SIZE)
        .offset((page - 1) * AUDIT_PAGE_SIZE)
    ).scalars())

    # Resolve actor ids → names from the whole user table (an actor may be a
    # platform admin acting on a tenant, so not necessarily in this scope).
    user_names = {u.id: (u.display_name or u.username)
                  for u in db.execute(select(User)).scalars()}
    tenant_names = _tenant_names(db)

    # Tenants this admin may filter by (all for platform, own for tenant admin).
    if allowed is None:
        selectable = list(db.execute(select(Tenant).order_by(Tenant.name)).scalars())
    elif allowed:
        selectable = list(db.execute(
            select(Tenant).where(Tenant.id.in_(allowed)).order_by(Tenant.name)).scalars())
    else:
        selectable = []
    tenants = [{"id": t.id, "name": t.name} for t in selectable]
    multi_tenant = len(tenants) > 1  # show the tenant column + picker only then

    # Filter dropdown options, scoped to what the caller may see.
    actor_stmt = select(User).order_by(User.username)
    action_stmt = select(AuditLog.action).distinct().order_by(AuditLog.action)
    if allowed is not None:
        actor_stmt = actor_stmt.where(User.tenant_id.in_(allowed))
        action_stmt = action_stmt.where(AuditLog.tenant_id.in_(allowed))
    actors = [{"id": u.id, "name": (u.display_name or u.username)}
              for u in db.execute(actor_stmt).scalars()]
    actions = list(db.execute(action_stmt).scalars())

    qparams = {k: v for k, v in {
        "action": action, "actor_id": actor_id, "tenant_id": tenant_id,
        "start": start, "end": end}.items() if v}

    return _templates(request).TemplateResponse(
        request, "settings/audit.html",
        {"user": user, "rows": rows,
         "is_platform_admin": is_platform_admin(user), "multi_tenant": multi_tenant,
         "user_names": user_names, "tenant_names": tenant_names,
         "tenants": tenants, "actors": actors, "actions": actions,
         "filters": {"action": action or "", "actor_id": actor_id or "",
                     "tenant_id": tenant_id or "", "start": start or "",
                     "end": end or ""},
         "base_query": urlencode(qparams),
         "page": page, "pages": pages, "total": total},
    )


@router.get("/audit/export", include_in_schema=False)
async def audit_export(
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_admin),
    format: str = "csv",
    action: str | None = None,
    actor_id: str | None = None,
    tenant_id: str | None = None,
    start: str | None = None,
    end: str | None = None,
):
    """Download the filtered audit log as CSV or JSON. Same access scoping as
    the viewer — a tenant admin can only export their own tenant's events."""
    fmt = (format or "csv").lower()
    if fmt not in ("csv", "json"):
        fmt = "csv"
    apply_fn, scope_tids, _allowed, _ = _resolve_audit_request(
        db, user, action=action, actor_id=actor_id, tenant_id=tenant_id,
        start=start, end=end)
    rows = list(db.execute(
        apply_fn(select(AuditLog)).order_by(AuditLog.ts.desc())).scalars())
    records = _audit_records(db, rows)

    # Exporting the audit trail is itself sensitive — record who pulled it.
    db.add(AuditLog(
        tenant_id=(next(iter(scope_tids)) if scope_tids and len(scope_tids) == 1 else None),
        actor_id=user.id, action="audit.export", target=f"format:{fmt}",
        details=f"rows={len(records)} scope={'all' if scope_tids is None else sorted(scope_tids)}",
        ip=request.client.host if request.client else None,
    ))
    db.commit()

    if fmt == "json":
        body = json.dumps(records, indent=2, default=str)
        return Response(body, media_type="application/json", headers={
            "Content-Disposition": 'attachment; filename="audit-log.json"'})
    cols = ["ts", "action", "actor_id", "actor", "tenant_id", "tenant",
            "target", "details", "ip"]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=cols)
    writer.writeheader()
    for rec in records:
        writer.writerow(rec)
    return Response(buf.getvalue(), media_type="text/csv", headers={
        "Content-Disposition": 'attachment; filename="audit-log.csv"'})
