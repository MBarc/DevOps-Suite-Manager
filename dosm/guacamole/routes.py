from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from dosm.auth.deps import require_user
from dosm.db import get_session
from dosm.guacamole.auth_json import AuthJsonCodec, build_connection_id, load_secret_key
from dosm.guacamole.builder import GuacamoleBuildError, build_connection
from dosm.guacamole.client import (
    GuacamoleClientError,
    GuacamoleUnreachable,
    fetch_session_token,
)
from dosm.hosts.repo import resolve_jump_chain
from dosm.jumps import get_tunnel_manager
from dosm.models import AuditLog, Host, User

router = APIRouter()


def _templates(request: Request):
    return request.app.state.templates


@router.get("/hosts/{host_id}/connect", response_class=HTMLResponse, include_in_schema=False)
async def host_connect(
    host_id: int,
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
) -> HTMLResponse:
    cfg = request.app.state.config
    gc = cfg.guacamole

    host = db.get(Host, host_id)
    if host is None:
        raise HTTPException(404)

    if not gc.enabled:
        return _templates(request).TemplateResponse(
            request,
            "guacamole/connect.html",
            {
                "host": host,
                "iframe_url": None,
                "error": (
                    "Guacamole is not enabled. Set guacamole.enabled: true in config.yaml "
                    "and point guacamole.base_url at your Guacamole instance."
                ),
                "user": user,
                "jump_chain": [],
                "tunnel_endpoint": None,
            },
        )

    chain = resolve_jump_chain(db, host)

    error: str | None = None
    iframe_url: str | None = None
    tunnel_lease = None
    endpoint_override: tuple[str, int] | None = None

    try:
        # If the target sits behind a jump, open (or reuse) a multiplexed
        # forward and tell Guacamole to connect to that local endpoint.
        if host.jump_host_id is not None:
            try:
                tunnel_lease = await get_tunnel_manager().acquire(
                    db, cfg, host, bind_host=gc.tunnel_bind_host
                )
            except Exception as e:
                raise GuacamoleBuildError(
                    f"failed to open jump tunnel chain: {e}"
                ) from e
            if tunnel_lease is not None:
                endpoint_override = (gc.dosm_reachable_host, tunnel_lease.bind_port)

        connection = build_connection(cfg, host, endpoint_override=endpoint_override)
        codec = AuthJsonCodec(load_secret_key(cfg.home / gc.secret_key_file))
        payload = {
            "username": user.username,
            "expires": int(time.time() + gc.session_ttl_seconds) * 1000,
            "connections": {
                connection.name: {
                    "protocol": connection.protocol,
                    "parameters": connection.parameters,
                }
            },
        }
        encoded = codec.encode(payload)
        try:
            token = await fetch_session_token(gc.base_url, encoded)
        except GuacamoleUnreachable as e:
            error = str(e)
        except GuacamoleClientError as e:
            error = str(e)
        else:
            cid = build_connection_id(connection.name, source="json")
            iframe_url = f"{gc.base_url.rstrip('/')}/#/client/{cid}?token={token}"
    except GuacamoleBuildError as e:
        error = str(e)

    # If we built a tunnel but the token exchange failed, release it now —
    # otherwise it stays leased for the user-visible session.
    if iframe_url is None and tunnel_lease is not None:
        try:
            await tunnel_lease.release()
        except Exception:
            pass

    db.add(
        AuditLog(
            actor_id=user.id,
            action="host.connect" if iframe_url else "host.connect.fail",
            target=f"host:{host.id}",
            details=(
                f"protocol={host.protocol}"
                + (f" jumps={len(chain)}" if chain else "")
                + (f" via_tunnel={tunnel_lease.bind_host}:{tunnel_lease.bind_port}"
                   if iframe_url and tunnel_lease else "")
                + (f" error={error[:120]}" if error else "")
            ),
            ip=request.client.host if request.client else None,
        )
    )

    return _templates(request).TemplateResponse(
        request,
        "guacamole/connect.html",
        {
            "host": host,
            "iframe_url": iframe_url,
            "error": error,
            "user": user,
            "jump_chain": chain,
            "tunnel_endpoint": (
                f"{tunnel_lease.bind_host}:{tunnel_lease.bind_port}" if tunnel_lease else None
            ),
        },
    )
