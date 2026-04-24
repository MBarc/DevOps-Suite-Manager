from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse

from dosm.auth.deps import require_user
from dosm.modules.builtin.system_info.snapshot import collect_snapshot, snapshot_dict


def build_router() -> APIRouter:
    router = APIRouter()

    @router.get("", response_class=HTMLResponse, include_in_schema=False)
    async def page(request: Request, user=Depends(require_user)):
        snap = collect_snapshot()
        return request.app.state.templates.TemplateResponse(
            request,
            "system_info/page.html",
            {"snap": snap, "user": user},
        )

    @router.get("/api/snapshot")
    async def api_snapshot(user=Depends(require_user)) -> JSONResponse:
        return JSONResponse(snapshot_dict())

    return router
