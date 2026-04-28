from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from dosm.auth.deps import require_user
from dosm.db import get_session
from dosm.docs_index.indexer import reindex_async
from dosm.models import AuditLog, Department, User

router = APIRouter(prefix="/org")


def _templates(request: Request):
    return request.app.state.templates


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _find_unique_slug(db: Session, base: str, exclude_id: int | None = None) -> str:
    slug = base
    n = 2
    while True:
        existing = db.execute(select(Department).where(Department.slug == slug)).scalar_one_or_none()
        if existing is None or (exclude_id is not None and existing.id == exclude_id):
            return slug
        slug = f"{base}-{n}"
        n += 1


def _sync_doc(cfg, dept: Department) -> None:
    """Write (or overwrite) the department's entry in the docs index."""
    if not dept.description:
        return
    org_dir: Path = cfg.docs_dir / "org"
    org_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        f"title: {dept.name}",
        f"folder: _unfiled",
        "---",
        "",
        f"# {dept.name}",
        "",
        dept.description,
        "",
    ]
    if dept.head:
        lines.append(f"**Department head:** {dept.head}  ")
    if dept.email:
        lines.append(f"**Contact:** {dept.email}  ")
    (org_dir / f"{dept.slug}.md").write_text("\n".join(lines), encoding="utf-8")


def _delete_doc(cfg, dept: Department) -> None:
    doc = cfg.docs_dir / "org" / f"{dept.slug}.md"
    if doc.exists():
        doc.unlink()


def _list_all(db: Session) -> list[Department]:
    return list(db.execute(select(Department).order_by(Department.name)).scalars())


# ── Tree JSON ─────────────────────────────────────────────────────────────────


@router.get("/tree.json", include_in_schema=False)
async def org_tree(
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
) -> JSONResponse:
    depts = _list_all(db)
    nodes: dict[int, dict] = {
        d.id: {"id": d.id, "name": d.name, "slug": d.slug,
               "head": d.head or "", "children": []}
        for d in depts
    }
    roots: list[dict] = []
    for d in depts:
        if d.parent_id and d.parent_id in nodes:
            nodes[d.parent_id]["children"].append(nodes[d.id])
        else:
            roots.append(nodes[d.id])

    if not roots:
        return JSONResponse({"name": "Organization", "slug": None, "children": []})
    if len(roots) == 1:
        return JSONResponse(roots[0])
    return JSONResponse({"name": "Organization", "slug": None, "children": roots})


# ── List ──────────────────────────────────────────────────────────────────────


@router.get("", response_class=HTMLResponse, include_in_schema=False)
async def org_list(
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    depts = _list_all(db)
    return _templates(request).TemplateResponse(
        request, "org/list.html", {"user": user, "depts": depts}
    )


# ── New ───────────────────────────────────────────────────────────────────────


@router.get("/new", response_class=HTMLResponse, include_in_schema=False)
async def org_new(
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    return _templates(request).TemplateResponse(
        request, "org/form.html",
        {"user": user, "dept": None, "parents": _list_all(db), "error": None}
    )


@router.post("/new", include_in_schema=False)
async def org_create(
    request: Request,
    name: str = Form(...),
    parent_id: str = Form(""),
    head: str = Form(""),
    email: str = Form(""),
    description: str = Form(""),
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    cfg = request.app.state.config
    name = name.strip()
    slug = _find_unique_slug(db, _slugify(name))
    pid = int(parent_id) if parent_id.strip() else None

    dept = Department(
        name=name,
        slug=slug,
        parent_id=pid,
        head=head.strip() or None,
        email=email.strip() or None,
        description=description.strip() or None,
    )
    try:
        db.add(dept)
        db.flush()
        db.add(AuditLog(actor_id=user.id, action="dept.create", target=f"dept:{slug}"))
        db.commit()
    except Exception as e:
        db.rollback()
        return _templates(request).TemplateResponse(
            request, "org/form.html",
            {"user": user, "dept": None, "parents": _list_all(db),
             "error": f"Could not create department: {e}"},
            status_code=400,
        )
    _sync_doc(cfg, dept)
    reindex_async(cfg, force=False)
    return RedirectResponse(f"/org/{slug}", status_code=303)


# ── Detail ────────────────────────────────────────────────────────────────────


@router.get("/{slug}", response_class=HTMLResponse, include_in_schema=False)
async def org_detail(
    slug: str,
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    dept = db.execute(select(Department).where(Department.slug == slug)).scalar_one_or_none()
    if dept is None:
        raise HTTPException(404)
    return _templates(request).TemplateResponse(
        request, "org/detail.html", {"user": user, "dept": dept}
    )


# ── Edit ──────────────────────────────────────────────────────────────────────


@router.get("/{slug}/edit", response_class=HTMLResponse, include_in_schema=False)
async def org_edit(
    slug: str,
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    dept = db.execute(select(Department).where(Department.slug == slug)).scalar_one_or_none()
    if dept is None:
        raise HTTPException(404)
    parents = [d for d in _list_all(db) if d.id != dept.id]
    return _templates(request).TemplateResponse(
        request, "org/form.html",
        {"user": user, "dept": dept, "parents": parents, "error": None}
    )


@router.post("/{slug}/edit", include_in_schema=False)
async def org_update(
    slug: str,
    request: Request,
    name: str = Form(...),
    parent_id: str = Form(""),
    head: str = Form(""),
    email: str = Form(""),
    description: str = Form(""),
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    cfg = request.app.state.config
    dept = db.execute(select(Department).where(Department.slug == slug)).scalar_one_or_none()
    if dept is None:
        raise HTTPException(404)

    dept.name = name.strip()
    dept.head = head.strip() or None
    dept.email = email.strip() or None
    dept.description = description.strip() or None
    pid = int(parent_id) if parent_id.strip() else None
    if pid != dept.id:
        dept.parent_id = pid

    try:
        db.add(AuditLog(actor_id=user.id, action="dept.update", target=f"dept:{slug}"))
        db.commit()
    except Exception as e:
        db.rollback()
        parents = [d for d in _list_all(db) if d.id != dept.id]
        return _templates(request).TemplateResponse(
            request, "org/form.html",
            {"user": user, "dept": dept, "parents": parents,
             "error": f"Could not update department: {e}"},
            status_code=400,
        )
    _sync_doc(cfg, dept)
    reindex_async(cfg, force=False)
    return RedirectResponse(f"/org/{dept.slug}", status_code=303)


# ── Delete ────────────────────────────────────────────────────────────────────


@router.post("/{slug}/delete", include_in_schema=False)
async def org_delete(
    slug: str,
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    cfg = request.app.state.config
    dept = db.execute(select(Department).where(Department.slug == slug)).scalar_one_or_none()
    if dept is None:
        raise HTTPException(404)
    _delete_doc(cfg, dept)
    db.delete(dept)
    db.add(AuditLog(actor_id=user.id, action="dept.delete", target=f"dept:{slug}"))
    db.commit()
    reindex_async(cfg, force=False)
    return RedirectResponse("/org", status_code=303)
