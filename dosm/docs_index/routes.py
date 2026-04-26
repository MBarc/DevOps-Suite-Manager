from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from dosm.auth.deps import require_user
from dosm.db import get_session
from dosm.docs_index.indexer import get_index_status, reindex_async
from dosm.docs_index.search import search as search_docs
from dosm.models import AuditLog, Document, User

router = APIRouter(prefix="/docs")


def _templates(request: Request):
    return request.app.state.templates


@router.get("", response_class=HTMLResponse, include_in_schema=False)
async def docs_home(
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    docs = list(
        db.execute(select(Document).order_by(Document.rel_path)).scalars()
    )
    return _templates(request).TemplateResponse(
        request,
        "docs/list.html",
        {"docs": docs, "status": get_index_status(), "user": user},
    )


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


@router.post("/reindex", include_in_schema=False)
async def docs_reindex(
    request: Request,
    force: int = 0,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    cfg = request.app.state.config
    reindex_async(cfg, force=bool(force))
    db.add(
        AuditLog(
            actor_id=user.id,
            action="docs.reindex",
            target="docs_index",
            details=f"force={bool(force)}",
        )
    )
    return RedirectResponse("/docs", status_code=303)


@router.get("/status", include_in_schema=False)
async def docs_status(user: User = Depends(require_user)):
    s = get_index_status()
    return JSONResponse(
        {
            "running": s.running,
            "total_files": s.total_files,
            "processed": s.processed,
            "indexed": s.indexed,
            "skipped_unchanged": s.skipped_unchanged,
            "errors": s.errors,
            "started_at": s.started_at.isoformat() if s.started_at else None,
            "finished_at": s.finished_at.isoformat() if s.finished_at else None,
            "embedder": s.embedder_name,
            "last_error": s.last_error,
            "messages": s.messages,
        }
    )


@router.get("/view", response_class=PlainTextResponse, include_in_schema=False)
async def docs_view(
    path: str,
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
) -> PlainTextResponse:
    """Return the raw document text. Kept intentionally plain: no markdown render,
    no templating — just what was on disk. The search page links here from hits."""
    cfg = request.app.state.config
    safe_root = cfg.docs_dir.resolve()
    target = (safe_root / path).resolve()
    if not str(target).startswith(str(safe_root) + "/") and target != safe_root:
        raise HTTPException(400, "path traversal rejected")
    if not target.exists() or not target.is_file():
        raise HTTPException(404)
    try:
        return PlainTextResponse(target.read_text(encoding="utf-8", errors="replace"))
    except OSError as e:
        raise HTTPException(500, f"read failed: {e}") from e
