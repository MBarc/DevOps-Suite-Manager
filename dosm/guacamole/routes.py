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

    if not gc.enabled:
        raise HTTPException(404, "Guacamole integration is not enabled in config.yaml")

    host = db.get(Host, host_id)
    if host is None:
        raise HTTPException(404)

    error: str | None = None
    iframe_url: str | None = None

    try:
        connection = build_connection(cfg, host)
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

    db.add(
        AuditLog(
            actor_id=user.id,
            action="host.connect" if iframe_url else "host.connect.fail",
            target=f"host:{host.id}",
            details=(
                f"protocol={host.protocol}"
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
        },
    )
