from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, WebSocket

from dosm.modules.builtin.system_info.snapshot import snapshot_dict

router = APIRouter(prefix="/metrics")

DEFAULT_INTERVAL_SECONDS = 2.0


@router.websocket("/ws")
async def metrics_ws(websocket: WebSocket, interval: float = DEFAULT_INTERVAL_SECONDS):
    """Stream DOSM-host metrics to the resource panel.

    Phase 7/8 will add additional data sources keyed by host id for remote
    targets; this endpoint is the local-host baseline.
    """
    uid = websocket.session.get("user_id") if websocket.session else None
    if uid is None:
        await websocket.close(code=4401)
        return

    await websocket.accept()
    loop = asyncio.get_running_loop()
    try:
        while True:
            # snapshot_dict sleeps 0.2s for cpu_percent; run in executor so
            # the event loop stays responsive.
            snap = await loop.run_in_executor(None, snapshot_dict)
            await websocket.send_text(json.dumps(snap))
            await asyncio.sleep(max(0.5, interval))
    except Exception:
        try:
            await websocket.close()
        except Exception:
            pass
