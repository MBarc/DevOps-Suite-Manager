from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from dosm.auth.deps import require_user
from dosm.config import update_config_yaml
from dosm.db import get_session
from dosm.models import AuditLog, User
from dosm.settings.cli_catalog import detect_all

router = APIRouter(prefix="/settings")


def _require_admin(user: User = Depends(require_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="settings require admin role")
    return user


def _templates(request: Request):
    return request.app.state.templates


@router.get("", response_class=HTMLResponse, include_in_schema=False)
async def settings_home(
    request: Request,
    user: User = Depends(_require_admin),
):
    cfg = request.app.state.config
    detected = detect_all(with_version=True)
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
    user: User = Depends(_require_admin),
):
    """Persist the cli_tools toggle map to config.yaml.

    The form submits an `enabled` checkbox per tool id; only checked ids are
    in the form-data, so we materialize the full map from the catalog and
    set False for anything not in the submitted form.
    """
    form = await request.form()
    submitted = set(form.getlist("enabled"))
    detected = detect_all(with_version=False)
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
