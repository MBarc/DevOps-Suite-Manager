from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from dosm.auth.deps import require_user
from dosm.modules.registry import get_registry

router = APIRouter(prefix="/modules")


def _templates(request: Request):
    return request.app.state.templates


@router.get("", response_class=HTMLResponse, include_in_schema=False)
async def modules_index(request: Request, user=Depends(require_user)):
    reg = get_registry()
    discovered = reg.discovered()
    loaded_names = {m.spec.name for m in reg.loaded()}
    errors = reg.errors()
    cfg = request.app.state.config
    enabled = set(cfg.enabled_modules)
    rows = []
    for d in discovered:
        rows.append(
            {
                "spec": d.spec,
                "source": d.source,
                "root": d.root,
                "enabled": d.spec.name in enabled,
                "loaded": d.spec.name in loaded_names,
                "error": errors.get(d.spec.name),
            }
        )
    return _templates(request).TemplateResponse(
        request, "modules/list.html", {"rows": rows, "user": user}
    )
