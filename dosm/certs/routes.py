from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from dosm.auth.deps import require_user
from dosm.db import get_session
from dosm.models import MonitoringSource, User
from dosm.monitoring.adapters import CertInfo, make_adapter
from dosm.secrets import SecretNotFound, get_backend

router = APIRouter(prefix="/certs")

_STATUS_ORDER = {"expired": 0, "critical": 1, "warn": 2, "ok": 3}
_cert_cache: tuple[list[CertInfo], datetime] | None = None
_CACHE_TTL = timedelta(minutes=5)


def peek_cached() -> tuple[list[CertInfo], datetime] | None:
    return _cert_cache


def _t(request: Request):
    return request.app.state.templates


async def _fetch_all(
    sources: list[MonitoringSource],
    backend,
    warn_days: int,
    critical_days: int,
) -> list[CertInfo]:
    results: list[CertInfo] = []
    for source in sources:
        try:
            token = backend.get_str(source.token_secret) if source.token_secret else ""
        except SecretNotFound:
            token = ""
        try:
            token2 = backend.get_str(source.token2_secret) if source.token2_secret else ""
        except SecretNotFound:
            token2 = ""
        adapter = make_adapter(source, token, token2)
        if adapter is None:
            continue
        try:
            certs = await adapter.fetch_certificates(warn_days=warn_days, critical_days=critical_days)
            results.extend(certs)
        except Exception:
            pass
    results.sort(key=lambda c: (_STATUS_ORDER.get(c.status, 9), c.not_after))
    return results


@router.get("", response_class=HTMLResponse)
async def certs_page(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    global _cert_cache
    cfg = request.app.state.config
    cc = cfg.certs
    now = datetime.now(UTC)
    error = None

    if _cert_cache is None or (now - _cert_cache[1]) >= _CACHE_TTL:
        sources = list(
            db.execute(
                select(MonitoringSource).where(MonitoringSource.enabled.is_(True))
            ).scalars()
        )
        if not sources:
            certs: list[CertInfo] = []
            error = "No monitoring sources enabled. Configure sources under Monitoring → Sources."
        else:
            backend = get_backend(cfg)
            try:
                certs = await _fetch_all(sources, backend, cc.expires_warn_days, cc.expires_critical_days)
                _cert_cache = (certs, now)
            except Exception as exc:
                certs = []
                error = str(exc)
    else:
        certs, _ = _cert_cache

    counts: dict[str, int] = {"expired": 0, "critical": 0, "warn": 0, "ok": 0}
    for c in certs:
        counts[c.status] = counts.get(c.status, 0) + 1

    return _t(request).TemplateResponse(
        request,
        "certs.html",
        {
            "user": user,
            "certs": certs,
            "error": error,
            "total": len(certs),
            "warn_days": cc.expires_warn_days,
            "critical_days": cc.expires_critical_days,
            **counts,
        },
    )


@router.post("/refresh", response_class=RedirectResponse)
async def certs_refresh(
    request: Request,
    user: User = Depends(require_user),
) -> RedirectResponse:
    global _cert_cache
    _cert_cache = None
    return RedirectResponse("/certs", status_code=303)
