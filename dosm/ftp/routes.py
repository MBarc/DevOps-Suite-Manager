"""Web file browser for FTP / FTPS / SFTP hosts (admin-only).

All operations go through ``get_file_backend(...)`` so the jump chain, secrets
resolution, and backend selection are identical to the CLI. Paths are relative
to the login home directory: every operation reconnects and lands in the home
dir, so an accumulated relative path like ``logs/2026`` is stable across calls
and works for both the FTP and SFTP backends.

Mutating operations (upload / delete / mkdir / rename) and downloads write an
``AuditLog`` row in the same request session, committed *after* the transfer so
the secrets backend never runs while this session holds a write lock.
"""
from __future__ import annotations

import tempfile

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from dosm.auth.deps import require_admin
from dosm.db import get_session
from dosm.ftp.base import FileTransferError
from dosm.ftp.service import (
    get_file_backend,
    host_has_file_transfer,
    transfer_between_hosts,
)
from dosm.models import AuditLog, Host, User

router = APIRouter(prefix="/files")

_DOWNLOAD_CHUNK = 64 * 1024


def _templates(request: Request):
    return request.app.state.templates


def _load_host(db: Session, host_id: int) -> Host:
    host = db.get(Host, host_id)
    if host is None:
        raise HTTPException(404, "host not found")
    if not host_has_file_transfer(host):
        raise HTTPException(
            400, f"file transfer is not configured on host {host.name!r}"
        )
    return host


def _join(cur: str, name: str) -> str:
    """Join a current dir and a child name into a home-relative path."""
    cur = cur.strip("/")
    return name if not cur else f"{cur}/{name}"


# ── Landing: pick a file-transfer host ───────────────────────────────────────
@router.get("", response_class=HTMLResponse, include_in_schema=False)
async def index(
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_admin),
):
    hosts = db.execute(
        select(Host)
        .where(Host.ft_method.isnot(None))
        .where(Host.ft_method != "")
        .order_by(Host.name)
    ).scalars().all()
    return _templates(request).TemplateResponse(
        request, "ftp/index.html", {"hosts": hosts, "user": user}
    )


# ── Browser page ─────────────────────────────────────────────────────────────
@router.get("/{host_id}", response_class=HTMLResponse, include_in_schema=False)
async def browser(
    host_id: int,
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_admin),
):
    host = _load_host(db, host_id)
    return _templates(request).TemplateResponse(
        request, "ftp/browser.html", {"host": host, "user": user}
    )


# ── JSON listing ─────────────────────────────────────────────────────────────
@router.get("/{host_id}/list", include_in_schema=False)
async def list_dir(
    host_id: int,
    request: Request,
    path: str = "",
    db: Session = Depends(get_session),
    user: User = Depends(require_admin),
):
    host = _load_host(db, host_id)
    backend = get_file_backend(request.app.state.config, db, host)
    try:
        entries = await backend.list_dir(path)
    except FileTransferError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    # Directories first, then case-insensitive by name.
    entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
    return JSONResponse({
        "path": path,
        "entries": [
            {"name": e.name, "is_dir": e.is_dir, "size": e.size, "modify": e.modify}
            for e in entries
        ],
    })


# ── Download ─────────────────────────────────────────────────────────────────
@router.get("/{host_id}/download", include_in_schema=False)
async def download(
    host_id: int,
    request: Request,
    path: str,
    db: Session = Depends(get_session),
    user: User = Depends(require_admin),
):
    host = _load_host(db, host_id)
    backend = get_file_backend(request.app.state.config, db, host)
    spool = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)
    try:
        await backend.retrieve(path, spool)
    except FileTransferError as e:
        spool.close()
        return JSONResponse({"error": str(e)}, status_code=502)

    size = spool.tell()
    spool.seek(0)
    db.add(AuditLog(
        actor_id=user.id, action="host.files.download",
        target=f"host:{host_id}", details=f"path={path} bytes={size}",
    ))
    db.commit()

    def _iter():
        try:
            while True:
                chunk = spool.read(_DOWNLOAD_CHUNK)
                if not chunk:
                    break
                yield chunk
        finally:
            spool.close()

    filename = path.rsplit("/", 1)[-1] or "download"
    return StreamingResponse(
        _iter(),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(size),
        },
    )


# ── Upload ───────────────────────────────────────────────────────────────────
@router.post("/{host_id}/upload", include_in_schema=False)
async def upload(
    host_id: int,
    request: Request,
    path: str = Form(""),
    file: UploadFile = File(...),
    db: Session = Depends(get_session),
    user: User = Depends(require_admin),
):
    host = _load_host(db, host_id)
    backend = get_file_backend(request.app.state.config, db, host)
    dest = _join(path, file.filename or "upload.bin")
    try:
        sent = await backend.store(dest, file.file)
    except FileTransferError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    db.add(AuditLog(
        actor_id=user.id, action="host.files.upload",
        target=f"host:{host_id}", details=f"path={dest} bytes={sent}",
    ))
    db.commit()
    return JSONResponse({"ok": True, "path": dest, "bytes": sent})


# ── Mkdir ────────────────────────────────────────────────────────────────────
@router.post("/{host_id}/mkdir", include_in_schema=False)
async def mkdir(
    host_id: int,
    request: Request,
    path: str = Form(""),
    name: str = Form(...),
    db: Session = Depends(get_session),
    user: User = Depends(require_admin),
):
    host = _load_host(db, host_id)
    backend = get_file_backend(request.app.state.config, db, host)
    target = _join(path, name.strip())
    try:
        await backend.mkdir(target)
    except FileTransferError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    db.add(AuditLog(
        actor_id=user.id, action="host.files.mkdir",
        target=f"host:{host_id}", details=f"path={target}",
    ))
    db.commit()
    return JSONResponse({"ok": True, "path": target})


# ── Delete (file or directory) ───────────────────────────────────────────────
@router.post("/{host_id}/delete", include_in_schema=False)
async def delete(
    host_id: int,
    request: Request,
    path: str = Form(...),
    is_dir: str = Form(""),
    db: Session = Depends(get_session),
    user: User = Depends(require_admin),
):
    host = _load_host(db, host_id)
    backend = get_file_backend(request.app.state.config, db, host)
    try:
        if is_dir:
            await backend.rmdir(path)
        else:
            await backend.delete(path)
    except FileTransferError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    db.add(AuditLog(
        actor_id=user.id, action="host.files.delete",
        target=f"host:{host_id}", details=f"path={path} dir={bool(is_dir)}",
    ))
    db.commit()
    return JSONResponse({"ok": True})


# ── Rename ───────────────────────────────────────────────────────────────────
@router.post("/{host_id}/rename", include_in_schema=False)
async def rename(
    host_id: int,
    request: Request,
    path: str = Form(""),
    src: str = Form(...),
    dst: str = Form(...),
    db: Session = Depends(get_session),
    user: User = Depends(require_admin),
):
    host = _load_host(db, host_id)
    backend = get_file_backend(request.app.state.config, db, host)
    src_path = _join(path, src)
    dst_path = _join(path, dst.strip())
    try:
        await backend.rename(src_path, dst_path)
    except FileTransferError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    db.add(AuditLog(
        actor_id=user.id, action="host.files.rename",
        target=f"host:{host_id}", details=f"{src_path} -> {dst_path}",
    ))
    db.commit()
    return JSONResponse({"ok": True, "path": dst_path})


# ── Host-to-host copy / move ─────────────────────────────────────────────────
@router.get("/{host_id}/targets", include_in_schema=False)
async def copy_targets(
    host_id: int,
    db: Session = Depends(get_session),
    user: User = Depends(require_admin),
):
    """Other hosts (besides this one) that have file transfer configured."""
    hosts = db.execute(
        select(Host)
        .where(Host.ft_method.isnot(None))
        .where(Host.ft_method != "")
        .where(Host.id != host_id)
        .order_by(Host.name)
    ).scalars().all()
    return JSONResponse({
        "targets": [{"id": h.id, "name": h.name, "method": h.ft_method} for h in hosts]
    })


@router.post("/{host_id}/copy", include_in_schema=False)
async def copy_to_host(
    host_id: int,
    request: Request,
    src: str = Form(...),
    dst_host_id: int = Form(...),
    dst_dir: str = Form(""),
    move: str = Form(""),
    db: Session = Depends(get_session),
    user: User = Depends(require_admin),
):
    src_host = _load_host(db, host_id)
    dst_host = _load_host(db, dst_host_id)
    basename = src.rsplit("/", 1)[-1]
    if not basename:
        return JSONResponse({"error": "source is not a file"}, status_code=400)
    dst_path = _join(dst_dir, basename)
    is_move = bool(move)
    try:
        sent = await transfer_between_hosts(
            request.app.state.config, db, src_host, src, dst_host, dst_path,
            move=is_move,
        )
    except FileTransferError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    db.add(AuditLog(
        actor_id=user.id,
        action="host.files.move" if is_move else "host.files.copy",
        target=f"host:{host_id}",
        details=f"{src} -> host:{dst_host_id}:{dst_path} bytes={sent}",
    ))
    db.commit()
    return JSONResponse({
        "ok": True, "bytes": sent, "dst_host": dst_host.name, "dst_path": dst_path,
        "moved": is_move,
    })
