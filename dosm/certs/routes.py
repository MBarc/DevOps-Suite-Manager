from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from dosm.auth.deps import require_user
from dosm.certs.scanner import get_by_thumbprint, scan_all
from dosm.models import User

router = APIRouter(prefix="/certs")


def _templates(request: Request):
    return request.app.state.templates


def _scan_context(request: Request, *, force: bool = False) -> dict:
    cfg = request.app.state.config
    cc = cfg.certs
    try:
        certs = scan_all(
            warn_days=cc.expires_warn_days,
            critical_days=cc.expires_critical_days,
            scan_paths=cc.scan_paths,
            windows_stores=cc.windows_stores,
            force=force,
        )
        error = None
    except Exception as exc:
        certs = []
        error = str(exc)

    counts = {"expired": 0, "critical": 0, "warn": 0, "ok": 0}
    for c in certs:
        counts[c.status] = counts.get(c.status, 0) + 1

    return {
        "certs": certs,
        "error": error,
        "total": len(certs),
        "warn_days": cc.expires_warn_days,
        "critical_days": cc.expires_critical_days,
        **counts,
    }


@router.get("", response_class=HTMLResponse)
async def certs_page(
    request: Request,
    user: User = Depends(require_user),
) -> HTMLResponse:
    ctx = _scan_context(request)
    return _templates(request).TemplateResponse(
        request, "certs.html", {"user": user, **ctx}
    )


@router.get("/{thumbprint}", response_class=HTMLResponse)
async def cert_detail(
    thumbprint: str,
    request: Request,
    user: User = Depends(require_user),
) -> HTMLResponse:
    cert = get_by_thumbprint(thumbprint)
    if cert is None:
        # Cache may have expired — trigger a fresh scan and retry once
        cfg = request.app.state.config
        cc = cfg.certs
        scan_all(
            warn_days=cc.expires_warn_days,
            critical_days=cc.expires_critical_days,
            scan_paths=cc.scan_paths,
            windows_stores=cc.windows_stores,
            force=True,
        )
        cert = get_by_thumbprint(thumbprint)
    if cert is None:
        raise HTTPException(status_code=404, detail="Certificate not found")
    return _templates(request).TemplateResponse(
        request, "certs_detail.html", {"user": user, "cert": cert}
    )


@router.post("/scan", response_class=RedirectResponse)
async def certs_rescan(
    request: Request,
    user: User = Depends(require_user),
) -> RedirectResponse:
    _scan_context(request, force=True)
    return RedirectResponse("/certs", status_code=303)
