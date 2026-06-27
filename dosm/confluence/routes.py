from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dosm.auth.deps import require_admin, require_user, user_has_role
from dosm.auth.tenancy import active_tenant_id, require_active_tenant
from dosm.confluence import DEPLOYMENTS, make_confluence_client, repo
from dosm.confluence.client import ConfluenceError
from dosm.confluence.sync import sync_listener
from dosm.credentials.access import visible_credentials
from dosm.db import get_session
from dosm.docs_index.vault import slugify
from dosm.models import AuditLog, ConfluenceListener, Credential, User

router = APIRouter(prefix="/settings/confluence")

# Credential kinds usable for Confluence auth: ``login`` (username=email +
# token-as-secret, for Cloud) or ``pat`` (token-as-secret, for Server/DC).
_CRED_KINDS = {"login", "pat"}


def _auth_credentials(db: Session, user: User, tid: int | None):
    return [c for c in visible_credentials(db, user, tid) if c.kind in _CRED_KINDS]


@router.get("", response_class=HTMLResponse, include_in_schema=False)
async def confluence_page(
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
    tid: int | None = Depends(active_tenant_id),
):
    return request.app.state.templates.TemplateResponse(
        request,
        "settings/confluence.html",
        {
            "user": user,
            "listeners": repo.list_listeners(db, tid),
            "credentials": _auth_credentials(db, user, tid),
            "deployments": DEPLOYMENTS,
            "can_edit": user_has_role(user, "tenant_admin"),
        },
    )


@router.post("/new", include_in_schema=False)
async def confluence_create(
    name: str = Form(...),
    deployment: str = Form(...),
    base_url: str = Form(...),
    space_key: str = Form(...),
    credential_id: str = Form(""),
    sync_pages: str | None = Form(None),
    sync_attachments: str | None = Form(None),
    db: Session = Depends(get_session),
    user: User = Depends(require_admin),
    tid: int = Depends(require_active_tenant),
):
    name = name.strip()
    deployment = deployment.strip()
    base_url = base_url.strip()
    space_key = space_key.strip()
    if not name or deployment not in DEPLOYMENTS or not base_url or not space_key:
        raise HTTPException(400, "name, deployment, base URL and space key are required")
    cred_id = int(credential_id) if credential_id.strip() else None
    if cred_id is None:
        raise HTTPException(400, "a credential is required")
    cred = db.get(Credential, cred_id)
    if cred is None or cred.tenant_id != tid:
        raise HTTPException(400, "credential not found")

    listener = ConfluenceListener(
        tenant_id=tid,
        name=name,
        deployment=deployment,
        base_url=base_url,
        space_key=space_key,
        slug=slugify(name),
        credential_id=cred_id,
        sync_pages=sync_pages is not None,
        sync_attachments=sync_attachments is not None,
        enabled=True,
    )
    db.add(listener)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(400, "a listener with that name or space already exists")
    db.add(AuditLog(
        tenant_id=tid, actor_id=user.id, action="settings.confluence.create",
        target=f"confluence_listener:{listener.id}",
        details=f"{deployment} {space_key} ({name})",
    ))
    db.commit()
    return RedirectResponse("/settings/confluence", status_code=303)


@router.post("/{listener_id}/toggle", include_in_schema=False)
async def confluence_toggle(
    listener_id: int,
    db: Session = Depends(get_session),
    user: User = Depends(require_admin),
    tid: int | None = Depends(active_tenant_id),
):
    row = repo.get_listener(db, listener_id, tid)
    if row is None:
        raise HTTPException(404)
    row.enabled = not row.enabled
    db.add(AuditLog(
        tenant_id=row.tenant_id, actor_id=user.id, action="settings.confluence.toggle",
        target=f"confluence_listener:{listener_id}", details=f"enabled={row.enabled}",
    ))
    db.commit()
    return RedirectResponse("/settings/confluence", status_code=303)


@router.post("/{listener_id}/delete", include_in_schema=False)
async def confluence_delete(
    listener_id: int,
    db: Session = Depends(get_session),
    user: User = Depends(require_admin),
    tid: int | None = Depends(active_tenant_id),
):
    row = repo.get_listener(db, listener_id, tid)
    if row is None:
        raise HTTPException(404)
    audit_tid = row.tenant_id
    db.delete(row)  # cascade removes its ConfluenceSyncItem rows
    db.add(AuditLog(
        tenant_id=audit_tid, actor_id=user.id, action="settings.confluence.delete",
        target=f"confluence_listener:{listener_id}",
    ))
    db.commit()
    return RedirectResponse("/settings/confluence", status_code=303)


@router.post("/{listener_id}/test", include_in_schema=False)
async def confluence_test(
    request: Request,
    listener_id: int,
    db: Session = Depends(get_session),
    user: User = Depends(require_admin),
    tid: int | None = Depends(active_tenant_id),
):
    row = repo.get_listener(db, listener_id, tid)
    if row is None:
        raise HTTPException(404)
    try:
        ok, message = await make_confluence_client(request.app.state.config, row).test_connection()
    except ConfluenceError as e:
        ok, message = False, str(e)
    except Exception as e:  # noqa: BLE001
        ok, message = False, f"{type(e).__name__}: {e}"
    return JSONResponse({"ok": ok, "message": message})


@router.post("/{listener_id}/sync-now", include_in_schema=False)
async def confluence_sync_now(
    request: Request,
    listener_id: int,
    db: Session = Depends(get_session),
    user: User = Depends(require_admin),
    tid: int | None = Depends(active_tenant_id),
):
    row = repo.get_listener(db, listener_id, tid)
    if row is None:
        raise HTTPException(404)
    try:
        result = await sync_listener(request.app.state.config, row, db)
    except ConfluenceError as e:
        return JSONResponse({"ok": False, "message": str(e)})
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "message": f"{type(e).__name__}: {e}"})
    db.add(AuditLog(
        tenant_id=row.tenant_id, actor_id=user.id, action="settings.confluence.sync",
        target=f"confluence_listener:{row.id}",
        details=(
            f"pages={result.pages_written} attachments={result.attachments_written} "
            f"deleted={result.deleted} errors={len(result.errors)}"
        ),
    ))
    db.commit()
    msg = (
        f"{result.pages_written} pages, {result.attachments_written} attachments, "
        f"{result.deleted} removed, {result.unchanged} unchanged"
    )
    if result.errors:
        msg += f" - {len(result.errors)} error(s): {result.errors[0]}"
    return JSONResponse({"ok": not result.errors, "message": msg})
