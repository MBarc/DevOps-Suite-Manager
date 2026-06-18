from __future__ import annotations

import asyncio
import csv
import io
import json
from functools import partial

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from dosm.auth.deps import ROLE_RANK, require_admin
from dosm.config import update_config_yaml
from dosm.db import get_session
from dosm.models import AuditLog, User
from dosm.settings.cli_catalog import detect_all

router = APIRouter(prefix="/settings")

# Valid DOSM roles, lowest-to-highest, for the RBAC mapping editor.
ROLES = tuple(sorted(ROLE_RANK, key=lambda r: ROLE_RANK[r]))


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


@router.get("/rbac", response_class=HTMLResponse, include_in_schema=False)
async def rbac_page(request: Request, user: User = Depends(require_admin)):
    cfg = request.app.state.config
    rows = sorted(
        ({"group": g, "role": r} for g, r in cfg.rbac.group_role_map.items()),
        key=lambda x: x["group"].lower(),
    )
    return _templates(request).TemplateResponse(
        request,
        "settings/rbac.html",
        {"user": user, "rows": rows, "default_role": cfg.rbac.default_role,
         "roles": list(ROLES), "okta_enabled": cfg.okta.enabled},
    )


@router.post("/rbac/mapping", include_in_schema=False)
async def rbac_mapping_save(
    request: Request,
    group: str = Form(...),
    role: str = Form(...),
    db: Session = Depends(get_session),
    user: User = Depends(require_admin),
):
    """Upsert one group → role assignment (add a new group or change an
    existing group's role - keyed on the exact group name)."""
    cfg = request.app.state.config
    group = group.strip()
    if not group:
        raise HTTPException(400, "group name is required")
    if role not in ROLE_RANK:
        raise HTTPException(400, f"invalid role {role!r}")
    new_map = dict(cfg.rbac.group_role_map)
    existed = group in new_map
    new_map[group] = role
    _save_rbac(cfg, group_role_map=new_map)
    db.add(AuditLog(
        actor_id=user.id,
        action="settings.rbac.mapping.update" if existed else "settings.rbac.mapping.add",
        target="rbac",
        details=f"group={group!r} role={role}",
    ))
    db.commit()
    return RedirectResponse("/settings/rbac?saved=1", status_code=303)


@router.post("/rbac/mapping/delete", include_in_schema=False)
async def rbac_mapping_delete(
    request: Request,
    group: str = Form(...),
    db: Session = Depends(get_session),
    user: User = Depends(require_admin),
):
    cfg = request.app.state.config
    new_map = dict(cfg.rbac.group_role_map)
    if new_map.pop(group, None) is None:
        raise HTTPException(404, "no such group mapping")
    _save_rbac(cfg, group_role_map=new_map)
    db.add(AuditLog(actor_id=user.id, action="settings.rbac.mapping.delete",
                    target="rbac", details=f"group={group!r}"))
    db.commit()
    return RedirectResponse("/settings/rbac?saved=1", status_code=303)


@router.post("/rbac/default", include_in_schema=False)
async def rbac_default_save(
    request: Request,
    default_role: str = Form(...),
    db: Session = Depends(get_session),
    user: User = Depends(require_admin),
):
    cfg = request.app.state.config
    # "none" denies access to users in no mapped group (group membership required).
    if default_role not in ROLE_RANK and default_role != "none":
        raise HTTPException(400, f"invalid role {default_role!r}")
    _save_rbac(cfg, default_role=default_role)
    db.add(AuditLog(actor_id=user.id, action="settings.rbac.default.update",
                    target="rbac", details=f"default_role={default_role}"))
    db.commit()
    return RedirectResponse("/settings/rbac?saved=1", status_code=303)


def _rbac_rows(cfg) -> list[dict]:
    return sorted(
        ({"group": g, "role": r} for g, r in cfg.rbac.group_role_map.items()),
        key=lambda x: x["group"].lower(),
    )


@router.get("/rbac/export.json", include_in_schema=False)
async def rbac_export_json(request: Request, user: User = Depends(require_admin)):
    cfg = request.app.state.config
    payload = {"default_role": cfg.rbac.default_role, "groups": _rbac_rows(cfg)}
    body = json.dumps(payload, indent=2)
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="dosm-rbac-groups.json"'},
    )


@router.get("/rbac/export.csv", include_in_schema=False)
async def rbac_export_csv(request: Request, user: User = Depends(require_admin)):
    cfg = request.app.state.config
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["group", "role"])
    for row in _rbac_rows(cfg):
        writer.writerow([row["group"], row["role"]])
    # Trailing row documents the fallback role for groups not listed above.
    writer.writerow(["(default - unmapped groups)", cfg.rbac.default_role])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="dosm-rbac-groups.csv"'},
    )
