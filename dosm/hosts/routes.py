from __future__ import annotations

import asyncio
import ipaddress
import socket

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dosm.auth.deps import require_user
from dosm.db import get_session
from dosm.hosts import repo
from dosm.hosts.repo import HostValidationError
from dosm.models import AuditLog, User

router = APIRouter(prefix="/hosts")

PROTOCOL_DEFAULT_PORTS = {"ssh": 22, "rdp": 3389, "vnc": 5900}


def _templates(request: Request):
    return request.app.state.templates


def _form_context(db: Session, user: User, host=None, error: str | None = None) -> dict:
    return {
        "host": host,
        "credentials": repo.list_credentials(db),
        "jump_candidates": repo.list_jump_candidates(
            db, exclude_host_id=host.id if host else None
        ),
        "protocols": list(repo.SUPPORTED_PROTOCOLS),
        "default_ports": PROTOCOL_DEFAULT_PORTS,
        "user": user,
        "error": error,
    }


@router.get("", response_class=HTMLResponse, include_in_schema=False)
async def hosts_list(
    request: Request,
    kind: str = "",
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    if kind not in ("", "servers", "jumpboxes"):
        kind = ""
    hosts = repo.list_hosts(db, kind=kind or None)
    credentials = repo.list_credentials(db)
    jump_candidates = repo.list_jump_candidates(db)
    n_servers, n_jumpboxes = repo.count_by_kind(db)
    return _templates(request).TemplateResponse(
        request, "hosts/list.html", {
            "hosts": hosts,
            "credentials": credentials,
            "jump_candidates": jump_candidates,
            "protocols": list(repo.SUPPORTED_PROTOCOLS),
            "kind": kind,
            "n_total": n_servers + n_jumpboxes,
            "n_servers": n_servers,
            "n_jumpboxes": n_jumpboxes,
            "user": user,
            "guacamole_enabled": request.app.state.config.guacamole.enabled,
        }
    )


@router.get("/new", response_class=HTMLResponse, include_in_schema=False)
async def hosts_new(
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    return _templates(request).TemplateResponse(
        request, "hosts/form.html", _form_context(db, user)
    )


@router.get("/resolve", include_in_schema=False)
async def resolve_host(
    q: str = "",
    user: User = Depends(require_user),
) -> JSONResponse:
    q = q.strip()
    if not q:
        return JSONResponse({})
    try:
        ipaddress.ip_address(q)
        is_ip = True
    except ValueError:
        is_ip = False
    try:
        if is_ip:
            hostname, _, _ = await asyncio.to_thread(socket.gethostbyaddr, q)
            return JSONResponse({"hostname": hostname, "ip": q})
        else:
            infos = await asyncio.to_thread(socket.getaddrinfo, q, None)
            ip = infos[0][4][0]
            return JSONResponse({"hostname": q, "ip": ip})
    except Exception:
        return JSONResponse({})


def _parse_int_or_none(v: str) -> int | None:
    return int(v) if v.strip() else None


@router.post("/new", include_in_schema=False)
async def hosts_create(
    request: Request,
    name: str = Form(...),
    hostname: str = Form(...),
    port: int = Form(22),
    protocol: str = Form("ssh"),
    description: str = Form(""),
    credential_id: str = Form(""),
    jump_host_id: str = Form(""),
    tags: str = Form(""),
    is_jumpbox: str | None = Form(None),
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    cred_id = _parse_int_or_none(credential_id)
    jump_id = _parse_int_or_none(jump_host_id)
    try:
        host = repo.create_host(
            db,
            name=name.strip(),
            hostname=hostname.strip(),
            port=port,
            protocol=protocol,
            description=description.strip() or None,
            credential_id=cred_id,
            jump_host_id=jump_id,
            tags_csv=tags,
            is_jumpbox=is_jumpbox is not None,
        )
    except (IntegrityError, HostValidationError) as e:
        db.rollback()
        return _templates(request).TemplateResponse(
            request,
            "hosts/form.html",
            _form_context(db, user, host=None, error=str(e.__cause__ or e)),
            status_code=400,
        )
    db.add(
        AuditLog(
            actor_id=user.id,
            action="host.create",
            target=f"host:{host.id}",
            details=(
                f"name={host.name} protocol={host.protocol}"
                + (f" jump={jump_id}" if jump_id else "")
            ),
        )
    )
    return RedirectResponse(f"/hosts/{host.id}", status_code=303)


@router.get("/{host_id}", response_class=HTMLResponse, include_in_schema=False)
async def hosts_detail(
    host_id: int,
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    host = repo.get_host(db, host_id)
    if host is None:
        raise HTTPException(404)
    chain = repo.resolve_jump_chain(db, host)
    return _templates(request).TemplateResponse(
        request, "hosts/detail.html", {"host": host, "jump_chain": chain, "user": user}
    )


@router.get("/{host_id}/edit", response_class=HTMLResponse, include_in_schema=False)
async def hosts_edit(
    host_id: int,
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    host = repo.get_host(db, host_id)
    if host is None:
        raise HTTPException(404)
    return _templates(request).TemplateResponse(
        request, "hosts/form.html", _form_context(db, user, host=host)
    )


@router.post("/{host_id}/edit", include_in_schema=False)
async def hosts_update(
    host_id: int,
    request: Request,
    name: str = Form(...),
    hostname: str = Form(...),
    port: int = Form(22),
    protocol: str = Form("ssh"),
    description: str = Form(""),
    credential_id: str = Form(""),
    jump_host_id: str = Form(""),
    tags: str = Form(""),
    is_jumpbox: str | None = Form(None),
    back: str = Form(""),
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    host = repo.get_host(db, host_id)
    if host is None:
        raise HTTPException(404)
    cred_id = _parse_int_or_none(credential_id)
    jump_id = _parse_int_or_none(jump_host_id)
    try:
        repo.update_host(
            db,
            host,
            name=name.strip(),
            hostname=hostname.strip(),
            port=port,
            protocol=protocol,
            description=description.strip() or None,
            credential_id=cred_id,
            jump_host_id=jump_id,
            tags_csv=tags,
            is_jumpbox=is_jumpbox is not None,
        )
    except (IntegrityError, HostValidationError) as e:
        db.rollback()
        return _templates(request).TemplateResponse(
            request,
            "hosts/form.html",
            _form_context(db, user, host=host, error=str(e.__cause__ or e)),
            status_code=400,
        )
    db.add(AuditLog(actor_id=user.id, action="host.update", target=f"host:{host.id}"))
    redirect_to = "/hosts" if back == "list" else f"/hosts/{host.id}"
    return RedirectResponse(redirect_to, status_code=303)


@router.post("/{host_id}/delete", include_in_schema=False)
async def hosts_delete(
    host_id: int,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    host = repo.get_host(db, host_id)
    if host is None:
        raise HTTPException(404)
    repo.delete_host(db, host)
    db.add(AuditLog(actor_id=user.id, action="host.delete", target=f"host:{host_id}"))
    return RedirectResponse("/hosts", status_code=303)
