from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from dosm.auth.deps import require_admin, require_user
from dosm.certs.sources import SUPPORTED_PROVIDERS, CertSourceError, get_cert_source
from dosm.db import get_session
from dosm.models import AuditLog, CertSource, MonitoringSource, User
from dosm.monitoring.adapters import CertInfo, make_adapter
from dosm.secrets import SecretNotFound, get_backend

router = APIRouter(prefix="/certs")

_STATUS_ORDER = {"expired": 0, "critical": 1, "warn": 2, "ok": 3}
_cert_cache: tuple[list[CertInfo], datetime] | None = None
_vault_cache: tuple[list[CertInfo], datetime] | None = None
_CACHE_TTL = timedelta(minutes=5)


def peek_cached() -> tuple[list[CertInfo], datetime] | None:
    return _cert_cache


async def _fetch_vault_certs(
    db: Session, cfg, warn_days: int, critical_days: int
) -> list[CertInfo]:
    """Fetch + merge certificates from every enabled cloud CertSource."""
    sources = list(
        db.execute(select(CertSource).where(CertSource.enabled.is_(True))).scalars()
    )
    results: list[CertInfo] = []
    for source in sources:
        try:
            adapter = get_cert_source(source, cfg)
            results.extend(
                await adapter.fetch_certificates(warn_days=warn_days, critical_days=critical_days)
            )
        except Exception:
            # A broken source shouldn't blank the whole dashboard.
            continue
    results.sort(key=lambda c: (_STATUS_ORDER.get(c.status, 9), c.not_after))
    return results


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
            error = "No monitoring sources enabled. Configure sources under Monitoring to Sources."
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

    # Vault certificates (cloud sources) - separate sub-section + cache.
    global _vault_cache
    if _vault_cache is None or (now - _vault_cache[1]) >= _CACHE_TTL:
        vault_certs = await _fetch_vault_certs(db, cfg, cc.expires_warn_days, cc.expires_critical_days)
        _vault_cache = (vault_certs, now)
    else:
        vault_certs, _ = _vault_cache
    vault_source_count = len(db.execute(
        select(CertSource.id).where(CertSource.enabled.is_(True))
    ).all())

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
            "vault_certs": vault_certs,
            "vault_source_count": vault_source_count,
            **counts,
        },
    )


@router.post("/refresh", response_class=RedirectResponse)
async def certs_refresh(
    request: Request,
    user: User = Depends(require_user),
) -> RedirectResponse:
    global _cert_cache, _vault_cache
    _cert_cache = None
    _vault_cache = None
    return RedirectResponse("/certs", status_code=303)


# ── Cloud certificate sources (Azure KV / AWS ACM / GCP Certificate Manager) ──
@router.get("/sources", response_class=HTMLResponse, include_in_schema=False)
async def cert_sources_page(
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_admin),
):
    from dosm.models import Credential

    sources = db.execute(select(CertSource).order_by(CertSource.name)).scalars().all()
    credentials = db.execute(select(Credential).order_by(Credential.name)).scalars().all()
    return _t(request).TemplateResponse(
        request,
        "certs_sources.html",
        {
            "user": user,
            "sources": sources,
            "credentials": credentials,
            "providers": SUPPORTED_PROVIDERS,
        },
    )


def _build_config(provider: str, form) -> str:
    """Assemble the non-secret provider config JSON from the submitted form."""
    keys = {
        "azure_kv": ["vault_url"],
        "aws_acm": ["region"],
        "gcp_certmgr": ["project", "location"],
    }.get(provider, [])
    cfg = {k: (form.get(k) or "").strip() for k in keys if (form.get(k) or "").strip()}
    return json.dumps(cfg)


@router.post("/sources/new", include_in_schema=False)
async def cert_source_create(
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_admin),
):
    global _vault_cache
    form = await request.form()
    name = (form.get("name") or "").strip()
    provider = (form.get("provider") or "").strip()
    auth_mode = (form.get("auth_mode") or "profile").strip()
    credential_id = form.get("credential_id") or ""
    if not name or provider not in SUPPORTED_PROVIDERS:
        raise HTTPException(400, "name and a supported provider are required")
    source = CertSource(
        name=name,
        provider=provider,
        config_json=_build_config(provider, form),
        auth_mode=auth_mode if auth_mode in ("profile", "ambient") else "profile",
        credential_id=int(credential_id) if credential_id else None,
        enabled=True,
    )
    db.add(source)
    db.flush()
    db.add(AuditLog(actor_id=user.id, action="certsource.create",
                    target=f"certsource:{source.id}", details=f"{provider} {name}"))
    db.commit()
    _vault_cache = None
    return RedirectResponse("/certs/sources", status_code=303)


@router.post("/sources/{source_id}/toggle", include_in_schema=False)
async def cert_source_toggle(
    source_id: int,
    db: Session = Depends(get_session),
    user: User = Depends(require_admin),
):
    global _vault_cache
    source = db.get(CertSource, source_id)
    if source is None:
        raise HTTPException(404)
    source.enabled = not source.enabled
    db.add(AuditLog(actor_id=user.id, action="certsource.update",
                    target=f"certsource:{source_id}", details=f"enabled={source.enabled}"))
    db.commit()
    _vault_cache = None
    return RedirectResponse("/certs/sources", status_code=303)


@router.post("/sources/{source_id}/delete", include_in_schema=False)
async def cert_source_delete(
    source_id: int,
    db: Session = Depends(get_session),
    user: User = Depends(require_admin),
):
    global _vault_cache
    source = db.get(CertSource, source_id)
    if source is None:
        raise HTTPException(404)
    db.delete(source)
    db.add(AuditLog(actor_id=user.id, action="certsource.delete", target=f"certsource:{source_id}"))
    db.commit()
    _vault_cache = None
    return RedirectResponse("/certs/sources", status_code=303)


@router.post("/sources/{source_id}/test", include_in_schema=False)
async def cert_source_test(
    request: Request,
    source_id: int,
    db: Session = Depends(get_session),
    user: User = Depends(require_admin),
):
    source = db.get(CertSource, source_id)
    if source is None:
        raise HTTPException(404)
    try:
        ok, message = await get_cert_source(source, request.app.state.config).test_connection()
    except CertSourceError as e:
        ok, message = False, str(e)
    except Exception as e:  # noqa: BLE001
        ok, message = False, f"{type(e).__name__}: {e}"
    return JSONResponse({"ok": ok, "message": message})
