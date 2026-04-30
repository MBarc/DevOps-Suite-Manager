from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from dosm.auth.deps import require_user
from dosm.db import get_session
from dosm.docs_index import applications as folder_repo
from dosm.docs_index import vault
from dosm.docs_index.indexer import get_index_status, reindex_async
from dosm.docs_index.markdown import render as render_markdown
from dosm.docs_index.search import search as search_docs
from dosm.models import AuditLog, Document, User

router = APIRouter(prefix="/docs")

_MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MiB


def _templates(request: Request):
    return request.app.state.templates


# ── Doc list ─────────────────────────────────────────────────────────────────


@router.get("", response_class=HTMLResponse, include_in_schema=False)
async def docs_home(
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    folders = folder_repo.list_folders(db)
    counts = {f.id: folder_repo.doc_count(db, f.id) for f in folders}
    unfiled = list(db.execute(
        select(Document)
        .where(Document.folder_id.is_(None))
        .order_by(Document.rel_path)
    ).scalars())
    return _templates(request).TemplateResponse(
        request,
        "docs/list.html",
        {
            "status": get_index_status(),
            "user": user,
            "folders": folders,
            "counts": counts,
            "unfiled": unfiled,
        },
    )


# ── Search ───────────────────────────────────────────────────────────────────


@router.get("/search", response_class=HTMLResponse, include_in_schema=False)
async def docs_search(
    request: Request,
    q: str = "",
    limit: int = 10,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    cfg = request.app.state.config
    hits = search_docs(db, cfg, q, limit=limit) if q else []
    return _templates(request).TemplateResponse(
        request,
        "docs/search.html",
        {"q": q, "hits": hits, "user": user, "status": get_index_status()},
    )


# ── Reindex ──────────────────────────────────────────────────────────────────


@router.post("/reindex", include_in_schema=False)
async def docs_reindex(
    request: Request,
    force: int = 0,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    cfg = request.app.state.config
    reindex_async(cfg, force=bool(force))
    db.add(AuditLog(actor_id=user.id, action="docs.reindex", target="docs_index", details=f"force={bool(force)}"))
    db.commit()
    return RedirectResponse("/docs", status_code=303)


@router.get("/status", include_in_schema=False)
async def docs_status(user: User = Depends(require_user)):
    s = get_index_status()
    return JSONResponse({
        "running": s.running, "total_files": s.total_files, "processed": s.processed,
        "indexed": s.indexed, "skipped_unchanged": s.skipped_unchanged, "errors": s.errors,
        "started_at": s.started_at.isoformat() if s.started_at else None,
        "finished_at": s.finished_at.isoformat() if s.finished_at else None,
        "embedder": s.embedder_name, "last_error": s.last_error, "messages": s.messages,
    })


# ── View ─────────────────────────────────────────────────────────────────────


@router.get("/view", include_in_schema=False)
async def docs_view(
    path: str,
    raw: bool = False,
    request: Request = None,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
) -> Response:
    cfg = request.app.state.config
    try:
        target = vault.resolve_path(cfg, path)
    except ValueError:
        raise HTTPException(400, "invalid path")
    if not target.exists() or not target.is_file():
        raise HTTPException(404)
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise HTTPException(500, f"read failed: {e}") from e

    is_md = target.suffix.lower() in {".md", ".markdown"}
    if raw or not is_md:
        return PlainTextResponse(text)

    _, body = vault.parse_frontmatter(text)
    rendered = render_markdown(body)
    doc = db.execute(select(Document).where(Document.rel_path == path)).scalar_one_or_none()
    return _templates(request).TemplateResponse(
        request,
        "docs/view.html",
        {
            "user": user,
            "path": path,
            "title": (doc.title if doc else None) or target.stem,
            "rendered_html": rendered,
            "doc": doc,
        },
    )


# ── Editor ───────────────────────────────────────────────────────────────────


@router.get("/new", response_class=HTMLResponse, include_in_schema=False)
async def docs_new(
    request: Request,
    app: str = "",
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    folders = folder_repo.list_folders(db)
    return _templates(request).TemplateResponse(
        request,
        "docs/editor.html",
        {
            "user": user,
            "folders": folders,
            "path": "",
            "title": "",
            "app_slug": app or vault.UNFILED_SLUG,
            "body": "",
            "original_mtime": "",
            "error": None,
        },
    )


@router.get("/edit", response_class=HTMLResponse, include_in_schema=False)
async def docs_edit(
    path: str,
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    cfg = request.app.state.config
    try:
        target = vault.resolve_path(cfg, path)
    except ValueError:
        raise HTTPException(400, "invalid path")
    if not target.exists() or not target.is_file():
        raise HTTPException(404)
    text = target.read_text(encoding="utf-8", errors="replace")
    fm, body = vault.parse_frontmatter(text)
    folders = folder_repo.list_folders(db)
    return _templates(request).TemplateResponse(
        request,
        "docs/editor.html",
        {
            "user": user,
            "folders": folders,
            "path": path,
            "title": fm.get("title", "") or target.stem,
            "app_slug": fm.get("folder", vault.UNFILED_SLUG),
            "body": body,
            "original_mtime": str(vault.file_mtime_ms(target)),
            "error": None,
        },
    )


@router.post("/save", include_in_schema=False)
async def docs_save(
    request: Request,
    path: str = Form(""),
    title: str = Form(...),
    app_slug: str = Form(vault.UNFILED_SLUG),
    body: str = Form(...),
    original_mtime: str = Form(""),
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    cfg = request.app.state.config
    title = title.strip() or "Untitled"
    app_slug = app_slug.strip() or vault.UNFILED_SLUG
    folders = folder_repo.list_folders(db)

    if path:
        # Editing existing doc — save to same path, don't move across folder dirs.
        try:
            target = vault.resolve_path(cfg, path)
        except ValueError:
            raise HTTPException(400, "invalid path")
        if not target.exists():
            raise HTTPException(404)
        # Stale-edit conflict detection.
        if original_mtime and str(vault.file_mtime_ms(target)) != original_mtime:
            return _templates(request).TemplateResponse(
                request,
                "docs/editor.html",
                {
                    "user": user, "folders": folders, "path": path,
                    "title": title, "app_slug": app_slug, "body": body,
                    "original_mtime": original_mtime,
                    "error": "This file was modified externally. Reload to see the latest version, or save anyway to overwrite.",
                    "conflict": True,
                },
                status_code=409,
            )
        rel_parts = Path(path)
        doc_slug = rel_parts.stem
        save_folder_slug = rel_parts.parent.name or vault.UNFILED_SLUG
        saved = vault.save_doc(
            cfg, folder_slug=save_folder_slug, doc_slug=doc_slug, title=title, body_md=body, author=user.username
        )
        action = "docs.update"
    else:
        # New doc.
        slug_base = vault.slugify(title)
        folder_dir = cfg.docs_dir / app_slug
        doc_slug = vault.find_unique_slug(folder_dir, slug_base)
        saved = vault.save_doc(cfg, folder_slug=app_slug, doc_slug=doc_slug, title=title, body_md=body, author=user.username)
        action = "docs.create"

    rel_saved = saved.relative_to(cfg.docs_dir).as_posix()
    db.add(AuditLog(actor_id=user.id, action=action, target=f"doc:{rel_saved}", details=f"title={title!r}"))
    db.commit()
    reindex_async(cfg, force=False)
    return RedirectResponse(f"/docs/view?path={rel_saved}", status_code=303)


@router.post("/convert", include_in_schema=False)
async def docs_convert(
    file: UploadFile = File(...),
    user: User = Depends(require_user),
):
    """Convert an uploaded file to markdown and return JSON — used by the editor's import button."""
    raw = await file.read()
    if len(raw) > _MAX_UPLOAD_BYTES:
        return JSONResponse({"error": "File too large"}, status_code=413)

    fname = file.filename or "upload"
    suffix = Path(fname).suffix.lower()
    warnings = ""
    title = Path(fname).stem

    try:
        if suffix == ".docx":
            body_md, warnings = vault.import_docx(raw)
        elif suffix == ".pdf":
            body_md = vault.import_pdf(raw)
        elif suffix in {".md", ".markdown", ".txt"}:
            text = raw.decode("utf-8", errors="replace")
            fm, body_md = vault.parse_frontmatter(text)
            if fm.get("title"):
                title = str(fm["title"])
        else:
            return JSONResponse({"error": f"Unsupported type: {suffix}. Supported: .docx .pdf .md .txt"}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    return JSONResponse({"title": title, "body_md": body_md, "warnings": warnings})


@router.post("/preview", include_in_schema=False)
async def docs_preview(
    body: str = Form(""),
    user: User = Depends(require_user),
):
    return JSONResponse({"html": render_markdown(body)})


@router.post("/delete", include_in_schema=False)
async def docs_delete(
    request: Request,
    path: str = Form(...),
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    cfg = request.app.state.config
    try:
        vault.delete_doc(cfg, path)
    except ValueError:
        raise HTTPException(400, "invalid path")
    except FileNotFoundError:
        raise HTTPException(404)
    db.add(AuditLog(actor_id=user.id, action="docs.delete", target=f"doc:{path}"))
    db.commit()
    reindex_async(cfg, force=False)
    return RedirectResponse("/docs", status_code=303)


# ── Import ───────────────────────────────────────────────────────────────────


@router.get("/import", response_class=HTMLResponse, include_in_schema=False)
async def docs_import_form(
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    folders = folder_repo.list_folders(db)
    return _templates(request).TemplateResponse(
        request,
        "docs/import.html",
        {"user": user, "folders": folders, "error": None},
    )


@router.post("/import", response_class=HTMLResponse, include_in_schema=False)
async def docs_import(
    request: Request,
    file: UploadFile = File(...),
    app_slug: str = Form(vault.UNFILED_SLUG),
    title_override: str = Form(""),
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    folders = folder_repo.list_folders(db)
    app_slug = app_slug.strip() or vault.UNFILED_SLUG
    raw = await file.read()
    if len(raw) > _MAX_UPLOAD_BYTES:
        return _templates(request).TemplateResponse(
            request,
            "docs/import.html",
            {"user": user, "folders": folders,
             "error": f"File too large ({len(raw) // 1024 // 1024} MiB). Limit is 25 MiB."},
            status_code=413,
        )

    fname = file.filename or "upload"
    suffix = Path(fname).suffix.lower()
    warnings = ""

    try:
        if suffix == ".docx":
            body_md, warnings = vault.import_docx(raw)
        elif suffix == ".pdf":
            body_md = vault.import_pdf(raw)
        elif suffix in {".md", ".markdown", ".txt"}:
            body_md = raw.decode("utf-8", errors="replace")
        else:
            return _templates(request).TemplateResponse(
                request,
                "docs/import.html",
                {"user": user, "folders": folders,
                 "error": f"Unsupported file type: {suffix!r}. Supported: .docx .pdf .md .txt"},
                status_code=400,
            )
    except Exception as e:
        return _templates(request).TemplateResponse(
            request,
            "docs/import.html",
            {"user": user, "folders": folders, "error": f"Conversion failed: {e}"},
            status_code=500,
        )

    title = title_override.strip() or Path(fname).stem
    # Pre-fill the editor for review — no file is committed yet.
    return _templates(request).TemplateResponse(
        request,
        "docs/editor.html",
        {
            "user": user,
            "folders": folders,
            "path": "",
            "title": title,
            "app_slug": app_slug,
            "body": body_md,
            "original_mtime": "",
            "error": None,
            "import_warnings": warnings or None,
        },
    )


# ── Folders ───────────────────────────────────────────────────────────────────


@router.get("/folders", response_class=HTMLResponse, include_in_schema=False)
async def folders_list(
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    folder_list = folder_repo.list_folders(db)
    counts = {f.id: folder_repo.doc_count(db, f.id) for f in folder_list}
    return _templates(request).TemplateResponse(
        request,
        "docs/applications.html",
        {"user": user, "folders": folder_list, "counts": counts, "error": None},
    )


@router.post("/folders", include_in_schema=False)
async def folders_create(
    request: Request,
    name: str = Form(...),
    slug: str = Form(""),
    description: str = Form(""),
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    name = name.strip()
    slug = slug.strip() or vault.slugify(name)
    if not slug:
        raise HTTPException(400, "slug is required")
    try:
        folder = folder_repo.create_folder(db, name=name, slug=slug, description=description or None)
        db.add(AuditLog(actor_id=user.id, action="folder.create", target=f"folder:{slug}"))
        db.commit()
    except Exception as e:
        db.rollback()
        folder_list = folder_repo.list_folders(db)
        counts = {f.id: folder_repo.doc_count(db, f.id) for f in folder_list}
        return _templates(request).TemplateResponse(
            request,
            "docs/applications.html",
            {"user": user, "folders": folder_list, "counts": counts,
             "error": f"Could not create folder: {e}"},
            status_code=400,
        )
    return RedirectResponse(f"/docs/folders/{folder.slug}", status_code=303)


@router.get("/folders/{slug}", response_class=HTMLResponse, include_in_schema=False)
async def folder_detail(
    slug: str,
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    folder = folder_repo.get_folder_by_slug(db, slug)
    if folder is None:
        raise HTTPException(404)
    docs = list(
        db.execute(
            select(Document)
            .where(Document.folder_id == folder.id)
            .order_by(Document.rel_path)
        ).scalars()
    )
    return _templates(request).TemplateResponse(
        request,
        "docs/application_detail.html",
        {"user": user, "folder": folder, "docs": docs},
    )


@router.post("/folders/{slug}/delete", include_in_schema=False)
async def folder_delete(
    slug: str,
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    folder = folder_repo.get_folder_by_slug(db, slug)
    if folder is None:
        raise HTTPException(404)
    folder_repo.delete_folder(db, folder)
    db.add(AuditLog(actor_id=user.id, action="folder.delete", target=f"folder:{slug}"))
    db.commit()
    return RedirectResponse("/docs/folders", status_code=303)
