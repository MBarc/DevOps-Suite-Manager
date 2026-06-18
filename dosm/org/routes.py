"""Organisation routes - AD-integrated department directory.

The org page is gated behind a one-time configuration: pick a Windows
jumpbox (an existing host with a credential profile) that DOSM will use
to run PowerShell ActiveDirectory cmdlets. Until that's set, the list view
shows an empty state pointing at /org/configure.

Members and parent hierarchy come from AD and are never user-edited; the
form collects only the AD group name, the manager's identifier, and an
optional free-text description (which still feeds the docs index).
"""
from __future__ import annotations

import asyncio
import re
from functools import partial
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from dosm.auth.deps import require_admin, require_user
from dosm.config import update_config_yaml
from dosm.db import get_session
from dosm.directory import (
    AdDirectoryError,
    AdDirectoryUnreachable,
    AdGroupNotFound,
    AdUserNotFound,
)
from dosm.directory.sync import resolve_inputs, sync_department
from dosm.docs_index.indexer import reindex_async
from dosm.hosts.repo import list_jump_candidates
from dosm.models import AuditLog, Department, DepartmentMember, Host, User

router = APIRouter(prefix="/org")


def _templates(request: Request):
    return request.app.state.templates


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _find_unique_slug(db: Session, base: str, exclude_id: int | None = None) -> str:
    slug = base
    n = 2
    while True:
        existing = db.execute(
            select(Department).where(Department.slug == slug)
        ).scalar_one_or_none()
        if existing is None or (exclude_id is not None and existing.id == exclude_id):
            return slug
        slug = f"{base}-{n}"
        n += 1


def _list_all(db: Session) -> list[Department]:
    return list(db.execute(select(Department).order_by(Department.name)).scalars())


def _is_configured(cfg) -> bool:
    # Mock adapter is self-contained - no jumpbox needed. Useful for dev,
    # tests, and demoing the UI when a real Windows jumpbox isn't available.
    if cfg.directory.adapter == "mock":
        return True
    return cfg.directory.ad_jumpbox_host_id is not None


def _jumpbox_host(db: Session, cfg) -> Host | None:
    hid = cfg.directory.ad_jumpbox_host_id
    if not hid:
        return None
    return db.get(Host, hid)


def _sync_doc(cfg, dept: Department, members: list[DepartmentMember]) -> None:
    """Write the department's contact-card markdown into the docs index.

    The agent picks this up on next reindex, so questions like "who's on
    the Helpdesk team" or "who do I email about X" can be answered from
    real AD data, not stale free-text notes.
    """
    org_dir: Path = cfg.docs_dir / "org"
    org_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        f"title: {dept.name}",
        "folder: _unfiled",
        "---",
        "",
        f"# {dept.name}",
        "",
    ]
    if dept.description:
        lines += [dept.description, ""]
    if dept.manager_name:
        lines.append(
            f"**Manager:** {dept.manager_name}"
            + (f" - {dept.manager_email}" if dept.manager_email else "")
            + (f" ({dept.manager_title})" if dept.manager_title else "")
        )
        lines.append("")
    if members:
        lines.append("## Members")
        lines.append("")
        for m in members:
            line = f"- {m.display_name}"
            if m.title:
                line += f" - {m.title}"
            if m.email:
                line += f" - {m.email}"
            if not m.enabled:
                line += " *(disabled)*"
            lines.append(line)
        lines.append("")
    (org_dir / f"{dept.slug}.md").write_text("\n".join(lines), encoding="utf-8")


def _delete_doc(cfg, dept: Department) -> None:
    doc = cfg.docs_dir / "org" / f"{dept.slug}.md"
    if doc.exists():
        doc.unlink()


# ─────────────────────────────────────────────────────────────────────────
# Tree JSON
# ─────────────────────────────────────────────────────────────────────────


@router.get("/tree.json", include_in_schema=False)
async def org_tree(
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
) -> JSONResponse:
    depts = _list_all(db)
    nodes: dict[int, dict] = {
        d.id: {
            "id": d.id,
            "name": d.name,
            "slug": d.slug,
            "manager": d.manager_name or "",
            "members": 0,
            "children": [],
        }
        for d in depts
    }
    # member counts in one query
    if depts:
        from sqlalchemy import func

        rows = db.execute(
            select(DepartmentMember.department_id, func.count(DepartmentMember.id))
            .group_by(DepartmentMember.department_id)
        ).all()
        for did, cnt in rows:
            if did in nodes:
                nodes[did]["members"] = int(cnt)

    roots: list[dict] = []
    for d in depts:
        if d.parent_id and d.parent_id in nodes:
            nodes[d.parent_id]["children"].append(nodes[d.id])
        else:
            roots.append(nodes[d.id])

    if not roots:
        return JSONResponse({"name": "Organisation", "slug": None, "children": []})
    if len(roots) == 1:
        return JSONResponse(roots[0])
    return JSONResponse({"name": "Organisation", "slug": None, "children": roots})


# ─────────────────────────────────────────────────────────────────────────
# List / empty state
# ─────────────────────────────────────────────────────────────────────────


@router.get("", response_class=HTMLResponse, include_in_schema=False)
async def org_list(
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    cfg = request.app.state.config
    jumpbox = _jumpbox_host(db, cfg)
    depts = _list_all(db)
    # Members joined with their dept, alphabetical by person name. The page
    # filters client-side so a typed query never round-trips.
    member_rows = db.execute(
        select(DepartmentMember, Department)
        .join(Department, DepartmentMember.department_id == Department.id)
        .order_by(DepartmentMember.display_name)
    ).all()
    return _templates(request).TemplateResponse(
        request,
        "org/list.html",
        {
            "user": user,
            "depts": depts,
            "members": [(m, d) for m, d in member_rows],
            "configured": _is_configured(cfg),
            "jumpbox": jumpbox,
        },
    )


# ─────────────────────────────────────────────────────────────────────────
# Configure (admin-only)
# ─────────────────────────────────────────────────────────────────────────


@router.get("/configure", response_class=HTMLResponse, include_in_schema=False)
async def org_configure(
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_admin),
):
    cfg = request.app.state.config
    candidates = list_jump_candidates(db)
    return _templates(request).TemplateResponse(
        request,
        "org/configure.html",
        {
            "user": user,
            "candidates": candidates,
            "selected_id": cfg.directory.ad_jumpbox_host_id,
            "current": _jumpbox_host(db, cfg),
            "use_mock": cfg.directory.adapter == "mock",
            "adapter": cfg.directory.adapter,
            "error": None,
            "test_result": None,
        },
    )


@router.post("/configure", include_in_schema=False)
async def org_configure_save(
    request: Request,
    host_id: str = Form(""),
    use_mock: str | None = Form(None),
    db: Session = Depends(get_session),
    user: User = Depends(require_admin),
):
    cfg = request.app.state.config
    mock_on = bool(use_mock)
    new_id: int | None = None if mock_on else (int(host_id) if host_id.strip() else None)
    adapter = "mock" if mock_on else "winrm_jumpbox"

    # Real-jumpbox path: validate the host exists and has a credential.
    if not mock_on and new_id is not None:
        host = db.get(Host, new_id)
        if host is None:
            raise HTTPException(400, f"host id {new_id} not found")
        if host.credential is None:
            return _templates(request).TemplateResponse(
                request,
                "org/configure.html",
                {
                    "user": user,
                    "candidates": list_jump_candidates(db),
                    "selected_id": new_id,
                    "current": _jumpbox_host(db, cfg),
                    "use_mock": mock_on,
                    "adapter": adapter,
                    "error": (
                        f"{host.name!r} has no credential profile attached. "
                        "Set one on the Hosts page first."
                    ),
                    "test_result": None,
                },
                status_code=400,
            )

    update_config_yaml(
        cfg.home,
        {
            "directory": {
                **cfg.directory.model_dump(),
                "adapter": adapter,
                "ad_jumpbox_host_id": new_id,
            }
        },
    )
    cfg.directory.adapter = adapter
    cfg.directory.ad_jumpbox_host_id = new_id
    db.add(
        AuditLog(
            actor_id=user.id,
            action="org.config.update",
            target="directory",
            details=f"adapter={adapter} ad_jumpbox_host_id={new_id}",
        )
    )
    db.commit()
    return RedirectResponse("/org/configure?saved=1", status_code=303)


@router.post("/configure/test", include_in_schema=False)
async def org_configure_test(
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_admin),
):
    cfg = request.app.state.config
    if not _is_configured(cfg):
        return _templates(request).TemplateResponse(
            request,
            "org/configure.html",
            {
                "user": user,
                "candidates": list_jump_candidates(db),
                "selected_id": None,
                "current": None,
                "use_mock": False,
                "adapter": cfg.directory.adapter,
                "error": "Save a configuration first, then test.",
                "test_result": None,
            },
            status_code=400,
        )
    # Run the sync test off the event loop - pywinrm is blocking.
    from dosm.directory import get_directory_source

    def _run() -> tuple[bool, str]:
        try:
            domain = get_directory_source(cfg).test_connection()
            return (True, f"OK - connected to AD domain {domain!r}")
        except Exception as e:
            return (False, f"{type(e).__name__}: {e}")

    ok, msg = await asyncio.get_event_loop().run_in_executor(None, _run)
    return _templates(request).TemplateResponse(
        request,
        "org/configure.html",
        {
            "user": user,
            "candidates": list_jump_candidates(db),
            "selected_id": cfg.directory.ad_jumpbox_host_id,
            "current": _jumpbox_host(db, cfg),
            "use_mock": cfg.directory.adapter == "mock",
            "adapter": cfg.directory.adapter,
            "error": None,
            "test_result": {"ok": ok, "message": msg},
        },
    )


# ─────────────────────────────────────────────────────────────────────────
# New / edit form
# ─────────────────────────────────────────────────────────────────────────


def _form_context(
    *,
    user: User,
    dept: Department | None,
    error: str | None = None,
    form_values: dict | None = None,
) -> dict:
    return {
        "user": user,
        "dept": dept,
        "error": error,
        "form": form_values or {},
    }


@router.get("/new", response_class=HTMLResponse, include_in_schema=False)
async def org_new(
    request: Request,
    user: User = Depends(require_user),
):
    cfg = request.app.state.config
    if not _is_configured(cfg):
        return RedirectResponse("/org", status_code=303)
    return _templates(request).TemplateResponse(
        request, "org/form.html", _form_context(user=user, dept=None)
    )


@router.post("/new", include_in_schema=False)
async def org_create(
    request: Request,
    name: str = Form(...),
    ad_group: str = Form(...),
    manager: str = Form(...),
    description: str = Form(""),
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    cfg = request.app.state.config
    if not _is_configured(cfg):
        raise HTTPException(400, "AD jumpbox not configured")

    name = name.strip()
    ad_group = ad_group.strip()
    manager = manager.strip()
    form_values = {
        "name": name,
        "ad_group": ad_group,
        "manager": manager,
        "description": description,
    }

    # Resolve AD inputs synchronously off the event loop. This validates
    # both the group and the manager exist before we write a row.
    try:
        group_dn, manager_dn, manager_attrs = await asyncio.get_event_loop().run_in_executor(
            None, partial(resolve_inputs, cfg, ad_group, manager)
        )
    except AdGroupNotFound:
        return _templates(request).TemplateResponse(
            request,
            "org/form.html",
            _form_context(
                user=user,
                dept=None,
                error=f"AD group {ad_group!r} was not found.",
                form_values=form_values,
            ),
            status_code=400,
        )
    except AdUserNotFound:
        return _templates(request).TemplateResponse(
            request,
            "org/form.html",
            _form_context(
                user=user,
                dept=None,
                error=f"Manager {manager!r} was not found in AD.",
                form_values=form_values,
            ),
            status_code=400,
        )
    except (AdDirectoryError, AdDirectoryUnreachable) as e:
        return _templates(request).TemplateResponse(
            request,
            "org/form.html",
            _form_context(
                user=user,
                dept=None,
                error=f"AD lookup failed: {e}",
                form_values=form_values,
            ),
            status_code=502,
        )

    slug = _find_unique_slug(db, _slugify(name) or "department")
    dept = Department(
        name=name,
        slug=slug,
        description=description.strip() or None,
        ad_group_name=ad_group,
        ad_group_dn=group_dn,
        manager_input=manager,
        manager_dn=manager_dn,
        manager_name=manager_attrs.get("name"),
        manager_email=manager_attrs.get("email"),
        manager_title=manager_attrs.get("title"),
        sync_status="pending",
    )
    db.add(dept)
    db.flush()
    db.add(AuditLog(actor_id=user.id, action="org.create", target=f"dept:{slug}"))
    db.commit()
    # Initial member sync immediately after create. If this fails, the dept
    # row stays - user can retry from the detail page.
    try:
        await asyncio.get_event_loop().run_in_executor(
            None, partial(sync_department, db, cfg, dept, actor_id=user.id)
        )
    except AdDirectoryError:
        pass  # error fields already populated; user sees banner on detail
    _sync_doc(cfg, dept, list(dept.members))
    reindex_async(cfg, force=False)
    return RedirectResponse(f"/org/{slug}", status_code=303)


# ─────────────────────────────────────────────────────────────────────────
# Detail / sync
# ─────────────────────────────────────────────────────────────────────────


@router.get("/{slug}", response_class=HTMLResponse, include_in_schema=False)
async def org_detail(
    slug: str,
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    dept = db.execute(
        select(Department).where(Department.slug == slug)
    ).scalar_one_or_none()
    if dept is None:
        raise HTTPException(404)
    members = list(
        db.execute(
            select(DepartmentMember)
            .where(DepartmentMember.department_id == dept.id)
            .order_by(DepartmentMember.display_name)
        ).scalars()
    )
    return _templates(request).TemplateResponse(
        request,
        "org/detail.html",
        {
            "user": user,
            "dept": dept,
            "members": members,
            "saved": request.query_params.get("saved"),
            "synced": request.query_params.get("synced"),
            "sync_error": request.query_params.get("err"),
        },
    )


@router.post("/{slug}/sync", include_in_schema=False)
async def org_sync(
    slug: str,
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    cfg = request.app.state.config
    dept = db.execute(
        select(Department).where(Department.slug == slug)
    ).scalar_one_or_none()
    if dept is None:
        raise HTTPException(404)
    if not _is_configured(cfg):
        raise HTTPException(400, "AD jumpbox not configured")
    try:
        await asyncio.get_event_loop().run_in_executor(
            None, partial(sync_department, db, cfg, dept, actor_id=user.id)
        )
    except AdDirectoryError as e:
        return RedirectResponse(
            f"/org/{slug}?err={str(e)[:200]}", status_code=303
        )
    members = list(
        db.execute(
            select(DepartmentMember).where(DepartmentMember.department_id == dept.id)
        ).scalars()
    )
    _sync_doc(cfg, dept, members)
    reindex_async(cfg, force=False)
    return RedirectResponse(f"/org/{slug}?synced=1", status_code=303)


# ─────────────────────────────────────────────────────────────────────────
# Edit
# ─────────────────────────────────────────────────────────────────────────


@router.get("/{slug}/edit", response_class=HTMLResponse, include_in_schema=False)
async def org_edit(
    slug: str,
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    dept = db.execute(
        select(Department).where(Department.slug == slug)
    ).scalar_one_or_none()
    if dept is None:
        raise HTTPException(404)
    return _templates(request).TemplateResponse(
        request,
        "org/form.html",
        _form_context(user=user, dept=dept),
    )


@router.post("/{slug}/edit", include_in_schema=False)
async def org_update(
    slug: str,
    request: Request,
    name: str = Form(...),
    ad_group: str = Form(...),
    manager: str = Form(...),
    description: str = Form(""),
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    cfg = request.app.state.config
    dept = db.execute(
        select(Department).where(Department.slug == slug)
    ).scalar_one_or_none()
    if dept is None:
        raise HTTPException(404)

    name = name.strip()
    ad_group = ad_group.strip()
    manager = manager.strip()
    form_values = {
        "name": name,
        "ad_group": ad_group,
        "manager": manager,
        "description": description,
    }

    # Re-resolve only if the inputs actually changed - avoids a WinRM round
    # trip when the user is just editing the description.
    group_changed = ad_group != dept.ad_group_name
    manager_changed = manager != dept.manager_input
    if group_changed or manager_changed:
        try:
            group_dn, manager_dn, manager_attrs = await asyncio.get_event_loop().run_in_executor(
                None, partial(resolve_inputs, cfg, ad_group, manager)
            )
        except AdGroupNotFound:
            return _templates(request).TemplateResponse(
                request,
                "org/form.html",
                _form_context(
                    user=user,
                    dept=dept,
                    error=f"AD group {ad_group!r} was not found.",
                    form_values=form_values,
                ),
                status_code=400,
            )
        except AdUserNotFound:
            return _templates(request).TemplateResponse(
                request,
                "org/form.html",
                _form_context(
                    user=user,
                    dept=dept,
                    error=f"Manager {manager!r} was not found in AD.",
                    form_values=form_values,
                ),
                status_code=400,
            )
        except (AdDirectoryError, AdDirectoryUnreachable) as e:
            return _templates(request).TemplateResponse(
                request,
                "org/form.html",
                _form_context(
                    user=user,
                    dept=dept,
                    error=f"AD lookup failed: {e}",
                    form_values=form_values,
                ),
                status_code=502,
            )
        dept.ad_group_name = ad_group
        dept.ad_group_dn = group_dn
        dept.manager_input = manager
        dept.manager_dn = manager_dn
        dept.manager_name = manager_attrs.get("name")
        dept.manager_email = manager_attrs.get("email")
        dept.manager_title = manager_attrs.get("title")
        dept.sync_status = "pending"

    if name != dept.name:
        dept.name = name
        # Slug stays - it's stable across renames, like Folder.
    dept.description = description.strip() or None

    db.add(AuditLog(actor_id=user.id, action="org.update", target=f"dept:{dept.slug}"))
    db.commit()
    _sync_doc(cfg, dept, list(dept.members))
    reindex_async(cfg, force=False)
    return RedirectResponse(f"/org/{dept.slug}?saved=1", status_code=303)


# ─────────────────────────────────────────────────────────────────────────
# Delete
# ─────────────────────────────────────────────────────────────────────────


@router.post("/{slug}/delete", include_in_schema=False)
async def org_delete(
    slug: str,
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    cfg = request.app.state.config
    dept = db.execute(
        select(Department).where(Department.slug == slug)
    ).scalar_one_or_none()
    if dept is None:
        raise HTTPException(404)
    _delete_doc(cfg, dept)
    # Children's parent_id will SET NULL via FK; they just become roots until
    # their own next sync re-derives parentage.
    db.delete(dept)
    db.add(AuditLog(actor_id=user.id, action="org.delete", target=f"dept:{slug}"))
    db.commit()
    reindex_async(cfg, force=False)
    return RedirectResponse("/org", status_code=303)


# ─────────────────────────────────────────────────────────────────────────
# People search
# ─────────────────────────────────────────────────────────────────────────


@router.get("/people", response_class=HTMLResponse, include_in_schema=False)
async def org_people(
    request: Request,
    q: str = "",
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    q = q.strip()
    results: list[tuple[DepartmentMember, Department]] = []
    if q:
        like = f"%{q}%"
        rows = db.execute(
            select(DepartmentMember, Department)
            .join(Department, DepartmentMember.department_id == Department.id)
            .where(
                or_(
                    DepartmentMember.display_name.ilike(like),
                    DepartmentMember.email.ilike(like),
                    DepartmentMember.title.ilike(like),
                )
            )
            .order_by(DepartmentMember.display_name)
            .limit(100)
        ).all()
        results = [(m, d) for m, d in rows]
    return _templates(request).TemplateResponse(
        request,
        "org/people.html",
        {"user": user, "q": q, "results": results},
    )
