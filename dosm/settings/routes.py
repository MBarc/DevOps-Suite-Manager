from __future__ import annotations

import asyncio
from functools import partial

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from dosm.auth.deps import require_admin
from dosm.config import update_config_yaml
from dosm.db import get_session
from dosm.models import AuditLog, User
from dosm.settings.cli_catalog import detect_all

router = APIRouter(prefix="/settings")


def _templates(request: Request):
    return request.app.state.templates


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
