from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, WebSocket
from sqlalchemy.orm import sessionmaker

from dosm.auth.tenancy import resolve_tenant_id
from dosm.db import get_engine
from dosm.metrics.sources import (
    LocalSource,
    MetricsError,
    MetricsSource,
    MetricsUnreachable,
    make_source_for_host,
)
from dosm.models import Host, User

router = APIRouter(prefix="/metrics")

DEFAULT_INTERVAL_SECONDS = 2.0


@router.websocket("/ws")
async def metrics_ws(
    websocket: WebSocket,
    interval: float = DEFAULT_INTERVAL_SECONDS,
    host_id: int | None = None,
):
    """Stream metrics. Without `host_id`, streams the DOSM host (LocalSource).
    With `host_id`, picks the matching MetricsSource (SSHSource for ssh hosts).
    """
    uid = websocket.session.get("user_id") if websocket.session else None
    if uid is None:
        await websocket.close(code=4401)
        return

    cfg = websocket.app.state.config

    # Resolve source under a short DB session.
    source: MetricsSource
    error_init: str | None = None
    if host_id is None:
        source = LocalSource()
    else:
        Session = sessionmaker(bind=get_engine(), future=True)
        with Session() as s:
            host = s.get(Host, host_id)
            user = s.get(User, uid)
            if host is None or user is None or not user.is_active:
                await websocket.close(code=4404)
                return
            # Tenant scope: a user must not stream metrics for another tenant's
            # host. Platform admins with an active tenant are confined to it;
            # with no active tenant (all-tenants view) tid is None and any host
            # is allowed (read-only overview).
            tid = resolve_tenant_id(websocket, user)
            if tid is not None and host.tenant_id != tid:
                await websocket.close(code=4404)
                return
            try:
                source = await make_source_for_host(cfg, host)
            except MetricsError as e:
                error_init = str(e)
                source = LocalSource()  # placeholder so the WS still works

    await websocket.accept()
    try:
        if error_init:
            await websocket.send_text(
                json.dumps({"_error": error_init, "_scope": "remote"})
            )
        consecutive_failures = 0
        while True:
            try:
                snap = await source.snapshot()
                consecutive_failures = 0
                await websocket.send_text(json.dumps(snap))
            except MetricsUnreachable as e:
                consecutive_failures += 1
                await websocket.send_text(
                    json.dumps({"_error": str(e), "_scope": getattr(source, "scope", "remote")})
                )
                # Back off after repeated failures to avoid hammering the host.
                await asyncio.sleep(min(interval * (2 ** min(consecutive_failures, 4)), 30))
                continue
            await asyncio.sleep(max(0.5, interval))
    except Exception:
        pass
    finally:
        try:
            await source.aclose()
        except Exception:
            pass
        try:
            await websocket.close()
        except Exception:
            pass
