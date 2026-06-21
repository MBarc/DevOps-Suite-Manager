"""Web routes for the 3-tier host organisation (application/environment/unit).

Mounted at ``/applications``. Viewing is open to any signed-in user; mutations
require operator+ (mirrors host management). Every mutation is audit-logged.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from dosm.applications import repo
from dosm.applications.repo import OrgValidationError
from dosm.auth.deps import require_operator, require_user
from dosm.auth.tenancy import active_tenant_id, require_active_tenant
from dosm.db import get_session
from dosm.models import AuditLog, User

router = APIRouter(prefix="/applications")


def _templates(request: Request):
    return request.app.state.templates


def _parse_int_or_none(v: str) -> int | None:
    v = (v or "").strip()
    return int(v) if v else None


def _render(request: Request, db: Session, user: User, tid: int | None,
            *, error: str | None = None, status: int = 200) -> HTMLResponse:
    return _templates(request).TemplateResponse(
        request,
        "applications/index.html",
        {
            "tree": repo.build_tree(db, tid),
            "tiers": repo.TIERS,
            "tier_labels": repo.TIER_LABELS,
            "child_tier": repo.CHILD_TIER,
            "user": user,
            "error": error,
        },
        status_code=status,
    )


@router.get("", response_class=HTMLResponse, include_in_schema=False)
async def applications_page(
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
    tid: int | None = Depends(active_tenant_id),
):
    return _render(request, db, user, tid)


@router.post("/units", include_in_schema=False)
async def create_unit(
    request: Request,
    name: str = Form(...),
    tier: str = Form(...),
    parent_id: str = Form(""),
    description: str = Form(""),
    db: Session = Depends(get_session),
    user: User = Depends(require_operator),
    tid: int = Depends(require_active_tenant),
):
    try:
        unit = repo.create_unit(
            db,
            tenant_id=tid,
            name=name,
            tier=tier,
            parent_id=_parse_int_or_none(parent_id),
            description=description or None,
        )
    except OrgValidationError as e:
        db.rollback()
        return _render(request, db, user, tid, error=str(e), status=400)
    db.add(
        AuditLog(
            tenant_id=tid,
            actor_id=user.id,
            action="orgunit.create",
            target=f"orgunit:{unit.id}",
            details=f"tier={unit.tier} name={unit.name} parent={unit.parent_id}",
        )
    )
    db.commit()
    return RedirectResponse("/applications", status_code=303)


@router.post("/units/{unit_id}/edit", include_in_schema=False)
async def edit_unit(
    unit_id: int,
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    db: Session = Depends(get_session),
    user: User = Depends(require_operator),
    tid: int | None = Depends(active_tenant_id),
):
    unit = repo.get_unit(db, unit_id, tid)
    if unit is None:
        raise HTTPException(404)
    try:
        repo.update_unit(db, unit, name=name, description=description or None)
    except OrgValidationError as e:
        db.rollback()
        return _render(request, db, user, tid, error=str(e), status=400)
    db.add(
        AuditLog(
            tenant_id=unit.tenant_id,
            actor_id=user.id,
            action="orgunit.update",
            target=f"orgunit:{unit.id}",
            details=f"name={unit.name}",
        )
    )
    db.commit()
    return RedirectResponse("/applications", status_code=303)


@router.post("/units/{unit_id}/delete", include_in_schema=False)
async def delete_unit(
    unit_id: int,
    db: Session = Depends(get_session),
    user: User = Depends(require_operator),
    tid: int | None = Depends(active_tenant_id),
):
    unit = repo.get_unit(db, unit_id, tid)
    if unit is None:
        raise HTTPException(404)
    audit_tid = unit.tenant_id
    descendants = len(repo.subtree_ids(db, unit)) - 1
    detail = f"tier={unit.tier} name={unit.name}"
    if descendants:
        detail += f" cascade={descendants}"
    repo.delete_unit(db, unit)
    db.add(
        AuditLog(
            tenant_id=audit_tid,
            actor_id=user.id,
            action="orgunit.delete",
            target=f"orgunit:{unit_id}",
            details=detail,
        )
    )
    db.commit()
    return RedirectResponse("/applications", status_code=303)
