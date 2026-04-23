from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dosm.auth.deps import require_user
from dosm.db import get_session
from dosm.hosts import repo
from dosm.models import AuditLog, User

router = APIRouter(prefix="/hosts")

PROTOCOL_DEFAULT_PORTS = {"ssh": 22, "rdp": 3389, "vnc": 5900}


def _templates(request: Request):
    return request.app.state.templates


@router.get("", response_class=HTMLResponse, include_in_schema=False)
async def hosts_list(
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    hosts = repo.list_hosts(db)
    return _templates(request).TemplateResponse(
        request, "hosts/list.html", {"hosts": hosts, "user": user}
    )


@router.get("/new", response_class=HTMLResponse, include_in_schema=False)
async def hosts_new(
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    return _templates(request).TemplateResponse(
        request,
        "hosts/form.html",
        {
            "host": None,
            "credentials": repo.list_credentials(db),
            "protocols": list(repo.SUPPORTED_PROTOCOLS),
            "default_ports": PROTOCOL_DEFAULT_PORTS,
            "user": user,
            "error": None,
        },
    )


@router.post("/new", include_in_schema=False)
async def hosts_create(
    request: Request,
    name: str = Form(...),
    hostname: str = Form(...),
    port: int = Form(22),
    protocol: str = Form("ssh"),
    description: str = Form(""),
    credential_id: str = Form(""),
    tags: str = Form(""),
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    cred_id = int(credential_id) if credential_id else None
    try:
        host = repo.create_host(
            db,
            name=name.strip(),
            hostname=hostname.strip(),
            port=port,
            protocol=protocol,
            description=description.strip() or None,
            credential_id=cred_id,
            tags_csv=tags,
        )
    except (IntegrityError, ValueError) as e:
        db.rollback()
        return _templates(request).TemplateResponse(
            request,
            "hosts/form.html",
            {
                "host": None,
                "credentials": repo.list_credentials(db),
                "protocols": list(repo.SUPPORTED_PROTOCOLS),
                "default_ports": PROTOCOL_DEFAULT_PORTS,
                "user": user,
                "error": str(e.__cause__ or e),
            },
            status_code=400,
        )
    db.add(
        AuditLog(
            actor_id=user.id,
            action="host.create",
            target=f"host:{host.id}",
            details=f"name={host.name} protocol={host.protocol}",
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
    return _templates(request).TemplateResponse(
        request, "hosts/detail.html", {"host": host, "user": user}
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
        request,
        "hosts/form.html",
        {
            "host": host,
            "credentials": repo.list_credentials(db),
            "protocols": list(repo.SUPPORTED_PROTOCOLS),
            "default_ports": PROTOCOL_DEFAULT_PORTS,
            "user": user,
            "error": None,
        },
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
    tags: str = Form(""),
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    host = repo.get_host(db, host_id)
    if host is None:
        raise HTTPException(404)
    cred_id = int(credential_id) if credential_id else None
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
            tags_csv=tags,
        )
    except (IntegrityError, ValueError) as e:
        db.rollback()
        return _templates(request).TemplateResponse(
            request,
            "hosts/form.html",
            {
                "host": host,
                "credentials": repo.list_credentials(db),
                "protocols": list(repo.SUPPORTED_PROTOCOLS),
                "default_ports": PROTOCOL_DEFAULT_PORTS,
                "user": user,
                "error": str(e.__cause__ or e),
            },
            status_code=400,
        )
    db.add(AuditLog(actor_id=user.id, action="host.update", target=f"host:{host.id}"))
    return RedirectResponse(f"/hosts/{host.id}", status_code=303)


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
