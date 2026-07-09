from __future__ import annotations

import asyncio
import ipaddress
import socket
import time

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dosm.applications import repo as org_repo
from dosm.auth.deps import require_operator, require_user
from dosm.auth.prefs import get_pref, set_pref
from dosm.auth.tenancy import active_tenant_id, require_active_tenant
from dosm.credentials.access import visible_credentials
from dosm.db import get_session
from dosm.hosts import repo
from dosm.hosts.repo import HostValidationError, resolve_jump_chain
from dosm.jumps import (
    JumpAuthError,
    JumpUnreachableError,
    TargetUnreachableError,
    get_tunnel_manager,
    probe_forward,
)
from dosm.models import AuditLog, NetworkPort, User

PING_TIMEOUT_SECONDS = 5.0

router = APIRouter(prefix="/hosts")

PROTOCOL_DEFAULT_PORTS = {"ssh": 22, "rdp": 3389, "vnc": 5900}
# Default ports for the file-transfer section (ftps = explicit AUTH TLS on 21).
FT_DEFAULT_PORTS = {"sftp": 22, "ftp": 21, "ftps": 21}


def _templates(request: Request):
    return request.app.state.templates


# Standard labels so the suggestions are useful even on a sparsely-seeded
# Port Library - merged with, and overridden by, the library's own entries.
# Fallback labels merged with the Port Library (library descriptions win).
# Implicit FTPS (990) is intentionally omitted - our FTPS is explicit on 21.
_STANDARD_PORT_LABELS = {
    22: "SSH / SFTP", 21: "FTP / explicit FTPS", 3389: "RDP", 5900: "VNC",
}


def _port_options(db: Session) -> list[tuple[int, str]]:
    """Port suggestions for the connection/FT port comboboxes, drawn from the
    Port Library and merged with the standard defaults. Sorted by port number."""
    labels = dict(_STANDARD_PORT_LABELS)
    for p in db.execute(
        select(NetworkPort).order_by(NetworkPort.port_number)
    ).scalars():
        labels[p.port_number] = p.description or labels.get(p.port_number, "")
    return sorted(labels.items())


def _org_units_payload(db: Session, tid: int | None) -> list[dict]:
    """Flat list of org units for the host form's cascading picker. The form's
    JS rebuilds the application -> environment -> unit selects from this."""
    return [
        {"id": u.id, "name": u.name, "tier": u.tier, "parent_id": u.parent_id}
        for u in org_repo.list_units(db, tid)
    ]


def _form_context(db: Session, user: User, tid: int | None, host=None,
                  error: str | None = None,
                  preset_org_unit_id: int | None = None) -> dict:
    port_opts = _port_options(db)
    return {
        "host": host,
        "org_units": _org_units_payload(db, tid),
        "preset_org_unit_id": preset_org_unit_id,
        "credentials": visible_credentials(db, user, tid),
        "jump_candidates": repo.list_jump_candidates(
            db, tid, exclude_host_id=host.id if host else None
        ),
        "protocols": list(repo.SUPPORTED_PROTOCOLS),
        "default_ports": PROTOCOL_DEFAULT_PORTS,
        "ft_methods": list(repo.FILE_TRANSFER_METHODS),
        "ft_default_ports": FT_DEFAULT_PORTS,
        "port_options": port_opts,
        "port_numbers": [n for n, _ in port_opts],
        "user": user,
        "error": error,
    }


@router.get("", include_in_schema=False)
async def hosts_list(
    request: Request,
    org_unit_id: str = "",
    user: User = Depends(require_user),
):
    """Retired: the standalone Hosts page now lives under the Inventory blade.
    Redirect to it, carrying any org-unit folder filter so deep links still work."""
    return RedirectResponse(
        "/inventory" + (f"?org_unit_id={org_unit_id}" if org_unit_id.strip() else ""),
        status_code=303,
    )


@router.get("/new", response_class=HTMLResponse, include_in_schema=False)
async def hosts_new(
    request: Request,
    org_unit_id: str = "",
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
    tid: int | None = Depends(active_tenant_id),
):
    # "Add host here" from the explorer prefills the org placement.
    preset = _parse_int_or_none(org_unit_id)
    if preset is not None and org_repo.get_unit(db, preset, tid) is None:
        preset = None
    return _templates(request).TemplateResponse(
        request, "hosts/form.html", _form_context(db, user, tid, preset_org_unit_id=preset)
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


def _friendly_integrity_error(e: IntegrityError) -> str:
    msg = str(e.__cause__ or e).lower()
    if "hosts.name" in msg:
        return "A host with that name already exists. Choose a different name."
    if "hosts.hostname" in msg:
        return "A host with that hostname/IP already exists."
    return "A duplicate value was rejected by the database."


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
    ft_method: str = Form(""),
    ft_port: str = Form(""),
    ft_credential_id: str = Form(""),
    org_unit_id: str = Form(""),
    db: Session = Depends(get_session),
    user: User = Depends(require_operator),
    tid: int = Depends(require_active_tenant),
):
    cred_id = _parse_int_or_none(credential_id)
    jump_id = _parse_int_or_none(jump_host_id)
    try:
        host = repo.create_host(
            db,
            tenant_id=tid,
            name=name.strip(),
            hostname=hostname.strip(),
            port=port,
            protocol=protocol,
            description=description.strip() or None,
            credential_id=cred_id,
            jump_host_id=jump_id,
            tags_csv=tags,
            is_jumpbox=is_jumpbox is not None,
            ft_method=ft_method or None,
            ft_port=_parse_int_or_none(ft_port),
            ft_credential_id=_parse_int_or_none(ft_credential_id),
            org_unit_id=_parse_int_or_none(org_unit_id),
        )
    except IntegrityError as e:
        db.rollback()
        return _templates(request).TemplateResponse(
            request,
            "hosts/form.html",
            _form_context(db, user, tid, host=None, error=_friendly_integrity_error(e)),
            status_code=400,
        )
    except HostValidationError as e:
        db.rollback()
        return _templates(request).TemplateResponse(
            request,
            "hosts/form.html",
            _form_context(db, user, tid, host=None, error=str(e)),
            status_code=400,
        )
    db.add(
        AuditLog(
            tenant_id=tid,
            actor_id=user.id,
            action="host.create",
            target=f"host:{host.id}",
            details=(
                f"name={host.name} protocol={host.protocol}"
                + (f" jump={jump_id}" if jump_id else "")
            ),
        )
    )
    db.commit()
    return RedirectResponse(f"/hosts/{host.id}", status_code=303)


@router.get("/{host_id}", response_class=HTMLResponse, include_in_schema=False)
async def hosts_detail(
    host_id: int,
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
    tid: int | None = Depends(active_tenant_id),
):
    host = repo.get_host(db, host_id, tid)
    if host is None:
        raise HTTPException(404)
    chain = repo.resolve_jump_chain(db, host)
    return _templates(request).TemplateResponse(
        request, "hosts/detail.html", {"host": host, "jump_chain": chain, "user": user}
    )


@router.post("/{host_id}/ping", include_in_schema=False)
async def hosts_ping(
    host_id: int,
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_operator),
    tid: int | None = Depends(active_tenant_id),
) -> JSONResponse:
    """TCP-probe ``host.hostname:host.port``, traversing the configured jump
    chain when present. This is a network reachability check on the protocol
    port - not ICMP - because ICMP doesn't traverse SSH tunnels and what the
    operator actually cares about is "can I reach the service through the
    jumps I configured."
    """
    host = repo.get_host(db, host_id, tid)
    if host is None:
        raise HTTPException(404)
    cfg = request.app.state.config
    target = f"{host.hostname}:{host.port}"

    if host.jump_host_id is None:
        started = time.monotonic()
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host.hostname, host.port),
                timeout=PING_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            return JSONResponse({
                "ok": False, "via": "direct", "target": target, "latency_ms": None,
                "message": f"timed out after {PING_TIMEOUT_SECONDS:.0f}s - "
                           f"host unreachable or port {host.port} filtered",
            })
        except OSError as e:
            return JSONResponse({
                "ok": False, "via": "direct", "target": target, "latency_ms": None,
                "message": f"connection refused or unreachable: {e}",
            })
        latency_ms = round((time.monotonic() - started) * 1000.0, 1)
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass
        return JSONResponse({
            "ok": True, "via": "direct", "target": target, "latency_ms": latency_ms,
            "message": f"reachable on {host.protocol} port",
        })

    chain = resolve_jump_chain(db, host)

    # RD Gateway topology (RDP target behind an RDP jumpbox): this is NOT an
    # SSH-tunnelled path - guacd connects to the gateway, which relays RDP to the
    # target (same detection as the Guacamole connect builder). Ping uses
    # SSH-tunnel semantics and so *cannot* reach the target directly; that's by
    # design, not a connectivity failure. The meaningful reachability signal here
    # is whether the RD Gateway itself answers, so probe the gateway and tell the
    # operator to use Connect for the full session test.
    if host.protocol == "rdp" and chain and chain[-1].protocol == "rdp":
        gateway = chain[-1]
        gw_port = gateway.port or 443
        gw_target = f"{gateway.hostname}:{gw_port}"
        started = time.monotonic()
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(gateway.hostname, gw_port),
                timeout=PING_TIMEOUT_SECONDS,
            )
        except (TimeoutError, OSError) as e:
            reason = (
                f"timed out after {PING_TIMEOUT_SECONDS:.0f}s"
                if isinstance(e, TimeoutError) else str(e)
            )
            return JSONResponse({
                "ok": False, "via": "rdgateway", "target": gw_target, "latency_ms": None,
                "message": f"RD Gateway {gateway.name!r} unreachable on port {gw_port} "
                           f"({reason}). The RDP session routes through this gateway, so it "
                           f"must be reachable. The target itself isn't pinged directly - "
                           f"use Connect to test the full RDP session.",
            })
        latency_ms = round((time.monotonic() - started) * 1000.0, 1)
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass
        return JSONResponse({
            "ok": True, "via": "rdgateway", "target": gw_target, "latency_ms": latency_ms,
            "message": f"RD Gateway {gateway.name!r} reachable on port {gw_port}. This host "
                       f"connects via RD Gateway, not an SSH tunnel, so ping validates the "
                       f"gateway only - use Connect to test the full RDP session.",
        })

    non_ssh = [h for h in chain if h.protocol != "ssh"]
    if non_ssh:
        names = ", ".join(f"{h.name!r} ({h.protocol})" for h in non_ssh)
        return JSONResponse({
            "ok": False, "via": "jump", "target": target, "latency_ms": None,
            "message": f"Ping only checks reachability through SSH-tunnelable jump chains. "
                       f"This chain has non-SSH hop(s): {names}. For RDP-over-RDP-jumpbox "
                       f"(RD Gateway) hosts use Connect to test the session instead.",
        })

    lease = None
    try:
        lease = await get_tunnel_manager().acquire(db, cfg, host)
    except JumpUnreachableError as e:
        return JSONResponse({
            "ok": False, "via": "jump", "target": target, "latency_ms": None,
            "message": str(e),
        })
    except JumpAuthError as e:
        return JSONResponse({
            "ok": False, "via": "jump", "target": target, "latency_ms": None,
            "message": str(e),
        })
    except Exception as e:
        return JSONResponse({
            "ok": False, "via": "jump", "target": target, "latency_ms": None,
            "message": f"failed to open jump tunnel: {e}",
        })

    try:
        started = time.monotonic()
        try:
            await probe_forward(lease, timeout=PING_TIMEOUT_SECONDS)
        except TargetUnreachableError as e:
            return JSONResponse({
                "ok": False, "via": "jump", "target": target, "latency_ms": None,
                "message": str(e),
            })
        latency_ms = round((time.monotonic() - started) * 1000.0, 1)
        return JSONResponse({
            "ok": True, "via": "jump", "target": target, "latency_ms": latency_ms,
            "message": f"reachable through {len(chain)} jump"
                       f"{'' if len(chain) == 1 else 's'}",
        })
    finally:
        try:
            await lease.release()
        except Exception:
            pass


@router.get("/{host_id}/edit", response_class=HTMLResponse, include_in_schema=False)
async def hosts_edit(
    host_id: int,
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
    tid: int | None = Depends(active_tenant_id),
):
    host = repo.get_host(db, host_id, tid)
    if host is None:
        raise HTTPException(404)
    return _templates(request).TemplateResponse(
        request, "hosts/form.html", _form_context(db, user, tid, host=host)
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
    ft_method: str = Form(""),
    ft_port: str = Form(""),
    ft_credential_id: str = Form(""),
    org_unit_id: str = Form(""),
    back: str = Form(""),
    db: Session = Depends(get_session),
    user: User = Depends(require_operator),
    tid: int | None = Depends(active_tenant_id),
):
    host = repo.get_host(db, host_id, tid)
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
            ft_method=ft_method or None,
            ft_port=_parse_int_or_none(ft_port),
            ft_credential_id=_parse_int_or_none(ft_credential_id),
            org_unit_id=_parse_int_or_none(org_unit_id),
        )
    except IntegrityError as e:
        db.rollback()
        return _templates(request).TemplateResponse(
            request,
            "hosts/form.html",
            _form_context(db, user, tid, host=host, error=_friendly_integrity_error(e)),
            status_code=400,
        )
    except HostValidationError as e:
        db.rollback()
        return _templates(request).TemplateResponse(
            request,
            "hosts/form.html",
            _form_context(db, user, tid, host=host, error=str(e)),
            status_code=400,
        )
    db.add(AuditLog(tenant_id=host.tenant_id, actor_id=user.id,
                    action="host.update", target=f"host:{host.id}"))
    db.commit()
    redirect_to = "/hosts" if back == "list" else f"/hosts/{host.id}"
    return RedirectResponse(redirect_to, status_code=303)


@router.post("/{host_id}/delete", include_in_schema=False)
async def hosts_delete(
    host_id: int,
    db: Session = Depends(get_session),
    user: User = Depends(require_operator),
    tid: int | None = Depends(active_tenant_id),
):
    host = repo.get_host(db, host_id, tid)
    if host is None:
        raise HTTPException(404)
    audit_tid = host.tenant_id
    repo.delete_host(db, host)
    db.add(AuditLog(tenant_id=audit_tid, actor_id=user.id,
                    action="host.delete", target=f"host:{host_id}"))
    db.commit()
    return RedirectResponse("/inventory", status_code=303)


@router.post("/{host_id}/assign-org", include_in_schema=False)
async def hosts_assign_org(
    host_id: int,
    org_unit_id: str = Form(""),
    db: Session = Depends(get_session),
    user: User = Depends(require_operator),
    tid: int | None = Depends(active_tenant_id),
) -> JSONResponse:
    """Reassign a host's org placement - the explorer's drag-and-drop onto a
    folder. An empty ``org_unit_id`` clears the assignment (drop on Unassigned).
    Returns JSON so the UI can update the card + counts in place."""
    host = repo.get_host(db, host_id, tid)
    if host is None:
        raise HTTPException(404)
    oid = _parse_int_or_none(org_unit_id)
    try:
        org_repo.assign_host(db, host, oid)
    except org_repo.OrgValidationError as e:
        db.rollback()
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    unit = org_repo.get_unit(db, oid, host.tenant_id) if oid else None
    path = unit.path_str if unit else None
    db.add(AuditLog(
        tenant_id=host.tenant_id,
        actor_id=user.id, action="host.update", target=f"host:{host.id}",
        details=f"org-assign -> {path or 'unassigned'}",
    ))
    db.commit()
    return JSONResponse({"ok": True, "org_unit_id": oid, "path": path})
