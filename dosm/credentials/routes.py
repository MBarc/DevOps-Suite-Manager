from __future__ import annotations

import re
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dosm.applications import repo as org_repo
from dosm.auth.deps import require_operator, require_user
from dosm.auth.prefs import get_pref, set_pref
from dosm.auth.tenancy import active_tenant_id, require_active_tenant
from dosm.credentials.access import (
    can_see_credential,
    visible_credentials_filter,
    visible_credentials_query,
)
from dosm.db import get_session
from dosm.models import AuditLog, Credential, Host, Tenant, User
from dosm.secrets import SecretNotFound, get_backend

VISIBILITIES = ("shared", "private")

router = APIRouter(prefix="/credentials")


def _parse_int_or_none(v: str) -> int | None:
    return int(v) if (v or "").strip() else None

CRED_KINDS = ("login", "ssh_key", "pat", "azure_sp", "aws_keys", "gcp_sa")

KIND_LABELS = {
    "login": "Login (username + password)",
    "ssh_key": "SSH Key",
    "pat": "Personal Access Token (PAT)",
    "azure_sp": "Azure service principal",
    "aws_keys": "AWS access keys",
    "gcp_sa": "GCP service account",
}


def _auto_secret_ref(name: str, tenant_slug: str) -> str:
    """Build the auto secret path. Credential names are unique *per tenant*, so
    the path is namespaced by tenant slug to avoid cross-tenant collisions on
    the (now non-unique) name slug."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower().strip()).strip("-")
    return f"t/{tenant_slug}/credentials/{slug}"


def _templates(request: Request):
    return request.app.state.templates


def _get_credential(db: Session, cred_id: int, tid: int | None) -> Credential | None:
    """Fetch a credential scoped to tenant ``tid``. Returns None when it belongs
    to a different tenant so callers 404 rather than leak existence."""
    cred = db.get(Credential, cred_id)
    if cred is None:
        return None
    if tid is not None and cred.tenant_id != tid:
        return None
    return cred


def _hosts_using(db: Session, cred_id: int) -> int:
    return int(
        db.execute(
            select(func.count()).select_from(Host).where(Host.credential_id == cred_id)
        ).scalar_one()
    )


@router.get("", response_class=HTMLResponse, include_in_schema=False)
async def credentials_list(
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
    tid: int | None = Depends(active_tenant_id),
):
    ou_id = _parse_int_or_none(request.query_params.get("org_unit_id", ""))
    view = request.query_params.get("view", "")
    if view in ("explorer", "table"):
        set_pref(db, user, "credentials_view", view)
    else:
        view = get_pref(user, "credentials_view", "explorer") or "explorer"
    if view not in ("explorer", "table"):
        view = "explorer"

    rows = list(db.execute(visible_credentials_query(user, tid)).scalars())
    enriched = []
    for c in rows:
        enriched.append(
            {
                "cred": c,
                "host_count": _hosts_using(db, c.id),
            }
        )

    if view == "explorer":
        vclause = visible_credentials_filter(user)
        extra = None if vclause is True else vclause
        tree = org_repo.build_tree(
            db, tid, counts=org_repo.direct_counts(db, tid, Credential, extra=extra))
        n_unassigned = sum(1 for c in rows if c.org_unit_id is None)
        return _templates(request).TemplateResponse(
            request, "credentials/explorer.html", {
                "rows": enriched, "tree": tree,
                "n_total": len(rows), "n_unassigned": n_unassigned,
                "initial_org_unit_id": ou_id, "user": user,
            })
    return _templates(request).TemplateResponse(
        request, "credentials/list.html", {"rows": enriched, "user": user}
    )


@router.post("/{cred_id}/assign-org", include_in_schema=False)
async def credentials_assign_org(
    cred_id: int,
    org_unit_id: str = Form(""),
    db: Session = Depends(get_session),
    user: User = Depends(require_operator),
    tid: int | None = Depends(active_tenant_id),
) -> JSONResponse:
    """Reassign a credential's org folder (explorer drag-and-drop). Empty
    ``org_unit_id`` clears it."""
    cred = _get_credential(db, cred_id, tid)
    if cred is None or not can_see_credential(user, cred):
        raise HTTPException(404)
    oid = _parse_int_or_none(org_unit_id)
    try:
        org_repo.assign_to_unit(db, cred, oid)
    except org_repo.OrgValidationError as e:
        db.rollback()
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    unit = org_repo.get_unit(db, oid, cred.tenant_id) if oid else None
    path = unit.path_str if unit else None
    db.add(AuditLog(tenant_id=cred.tenant_id, actor_id=user.id, action="credential.update",
                    target=f"credential:{cred.id}", details=f"org-assign -> {path or 'unassigned'}"))
    db.commit()
    return JSONResponse({"ok": True, "org_unit_id": oid, "path": path})


def _form_context(host=None, error: str | None = None, secret_present: bool = False, **overrides) -> dict:
    base = {
        "cred": host,
        "kinds": list(CRED_KINDS),
        "kind_labels": KIND_LABELS,
        "error": error,
        "secret_present": secret_present,
    }
    base.update(overrides)
    return base


@router.get("/new", response_class=HTMLResponse, include_in_schema=False)
async def credentials_new(
    request: Request,
    org_unit_id: str = "",
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
    tid: int | None = Depends(active_tenant_id),
):
    return _templates(request).TemplateResponse(
        request,
        "credentials/form.html",
        _form_context(user=user, org_units=org_repo.list_units(db, tid),
                      preset_org_unit_id=_parse_int_or_none(org_unit_id)),
    )


@router.post("/new", include_in_schema=False)
async def credentials_create(
    request: Request,
    name: str = Form(...),
    kind: str = Form(...),
    username: str = Form(""),
    domain: str = Form(""),
    secret_ref: str = Form(""),
    secret_value: str = Form(""),
    org_unit_id: str = Form(""),
    visibility: str = Form("shared"),
    db: Session = Depends(get_session),
    user: User = Depends(require_operator),
    tid: int = Depends(require_active_tenant),
):
    cfg = request.app.state.config
    name = name.strip()
    tenant = db.get(Tenant, tid)
    tenant_slug = tenant.slug if tenant is not None else str(tid)
    secret_ref = secret_ref.strip() or _auto_secret_ref(name, tenant_slug)
    visibility = visibility if visibility in VISIBILITIES else "shared"
    if kind not in CRED_KINDS:
        return _templates(request).TemplateResponse(
            request,
            "credentials/form.html",
            _form_context(user=user, error=f"unknown kind {kind!r}"),
            status_code=400,
        )
    if not name:
        return _templates(request).TemplateResponse(
            request,
            "credentials/form.html",
            _form_context(user=user, error="Profile name is required."),
            status_code=400,
        )
    cred = Credential(
        tenant_id=tid,
        name=name,
        kind=kind,
        username=username.strip() or None,
        domain=domain.strip() or None,
        secret_ref=secret_ref,
        org_unit_id=_parse_int_or_none(org_unit_id),
        owner_id=user.id,
        visibility=visibility,
    )
    db.add(cred)
    try:
        db.flush()
    except IntegrityError as e:
        db.rollback()
        return _templates(request).TemplateResponse(
            request,
            "credentials/form.html",
            _form_context(user=user, error=str(e.__cause__ or e)),
            status_code=400,
        )
    cid = cred.id
    db.add(
        AuditLog(
            tenant_id=tid,
            actor_id=user.id,
            action="credential.create",
            target=f"credential:{cid}",
            details=f"kind={kind} secret_ref={secret_ref} visibility={visibility} inline_secret={'yes' if secret_value else 'no'}",
        )
    )
    # Commit the credential row + audit before opening a second session for
    # the secrets backend, so SQLite's single-writer doesn't deadlock with us.
    db.commit()

    if secret_value:
        try:
            get_backend(cfg).set_str(secret_ref, secret_value)
        except Exception as e:
            db.add(
                AuditLog(
                    tenant_id=tid,
                    actor_id=user.id,
                    action="credential.create.partial",
                    target=f"credential:{cid}",
                    details=f"secret write failed: {e}",
                )
            )
            return RedirectResponse(f"/credentials/{cid}?warn=secret-write-failed", status_code=303)

    return RedirectResponse(f"/credentials/{cid}", status_code=303)


@router.get("/{cred_id}", response_class=HTMLResponse, include_in_schema=False)
async def credentials_detail(
    cred_id: int,
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
    tid: int | None = Depends(active_tenant_id),
):
    cred = _get_credential(db, cred_id, tid)
    if cred is None or not can_see_credential(user, cred):
        raise HTTPException(404)
    cfg = request.app.state.config
    secret_present = False
    try:
        get_backend(cfg).get(cred.secret_ref)
        secret_present = True
    except SecretNotFound:
        secret_present = False
    except Exception:
        secret_present = False
    hosts = list(
        db.execute(
            select(Host).where(Host.credential_id == cred.id).order_by(Host.name)
        ).scalars()
    )
    return _templates(request).TemplateResponse(
        request,
        "credentials/detail.html",
        {"cred": cred, "secret_present": secret_present, "hosts": hosts, "user": user},
    )


@router.get("/{cred_id}/edit", response_class=HTMLResponse, include_in_schema=False)
async def credentials_edit(
    cred_id: int,
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_operator),
    tid: int | None = Depends(active_tenant_id),
):
    cred = _get_credential(db, cred_id, tid)
    if cred is None or not can_see_credential(user, cred):
        raise HTTPException(404)
    return _templates(request).TemplateResponse(
        request,
        "credentials/form.html",
        _form_context(host=cred, user=user, org_units=org_repo.list_units(db, tid),
                      preset_org_unit_id=cred.org_unit_id),
    )


@router.post("/{cred_id}/edit", include_in_schema=False)
async def credentials_update(
    cred_id: int,
    request: Request,
    name: str = Form(...),
    kind: str = Form(...),
    username: str = Form(""),
    domain: str = Form(""),
    secret_value: str = Form(""),
    org_unit_id: str = Form(""),
    visibility: str = Form(""),
    db: Session = Depends(get_session),
    user: User = Depends(require_operator),
    tid: int | None = Depends(active_tenant_id),
):
    cfg = request.app.state.config
    cred = _get_credential(db, cred_id, tid)
    if cred is None or not can_see_credential(user, cred):
        raise HTTPException(404)
    if kind not in CRED_KINDS:
        return _templates(request).TemplateResponse(
            request,
            "credentials/form.html",
            _form_context(host=cred, user=user, error=f"unknown kind {kind!r}"),
            status_code=400,
        )
    new_visibility = visibility if visibility in VISIBILITIES else cred.visibility
    visibility_changed = new_visibility != cred.visibility
    cred.name = name.strip()
    cred.kind = kind
    cred.username = username.strip() or None
    cred.domain = domain.strip() or None
    cred.org_unit_id = _parse_int_or_none(org_unit_id)
    cred.visibility = new_visibility
    # secret_ref is immutable after creation - keeps existing value
    cred.updated_at = datetime.now(UTC)
    try:
        db.flush()
    except IntegrityError as e:
        db.rollback()
        return _templates(request).TemplateResponse(
            request,
            "credentials/form.html",
            _form_context(host=cred, user=user, error=str(e.__cause__ or e)),
            status_code=400,
        )
    audit_tid = cred.tenant_id
    db.add(AuditLog(tenant_id=audit_tid, actor_id=user.id, action="credential.update", target=f"credential:{cred.id}"))
    if visibility_changed:
        db.add(
            AuditLog(
                tenant_id=audit_tid,
                actor_id=user.id,
                action="credential.visibility",
                target=f"credential:{cred.id}",
                details=f"visibility={new_visibility}",
            )
        )
    cred_id_local = cred.id
    secret_ref_local = cred.secret_ref
    db.commit()  # release the writer before talking to the secrets backend
    if secret_value:
        try:
            get_backend(cfg).set_str(secret_ref_local, secret_value)
        except Exception as e:
            with __import__("dosm.db", fromlist=["session_scope"]).session_scope() as s2:
                s2.add(
                    AuditLog(
                        tenant_id=audit_tid,
                        actor_id=user.id,
                        action="credential.update.partial",
                        target=f"credential:{cred_id_local}",
                        details=f"secret write failed: {e}",
                    )
                )
    return RedirectResponse(f"/credentials/{cred_id_local}", status_code=303)


@router.post("/{cred_id}/delete", include_in_schema=False)
async def credentials_delete(
    cred_id: int,
    db: Session = Depends(get_session),
    user: User = Depends(require_operator),
    tid: int | None = Depends(active_tenant_id),
):
    cred = _get_credential(db, cred_id, tid)
    if cred is None or not can_see_credential(user, cred):
        raise HTTPException(404)
    if _hosts_using(db, cred.id) > 0:
        # Refuse rather than orphan host references silently.
        raise HTTPException(409, "credential is in use by one or more hosts")
    name = cred.name
    audit_tid = cred.tenant_id
    db.delete(cred)
    db.add(AuditLog(tenant_id=audit_tid, actor_id=user.id, action="credential.delete", target=f"credential:{cred_id}", details=f"name={name}"))
    db.commit()
    return RedirectResponse("/credentials", status_code=303)
