from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from dosm.auth.deps import require_user
from dosm.db import get_session
from dosm.hosts.repo import list_hosts
from dosm.models import AuditLog, MonitoringSource, User
from dosm.monitoring import repo
from dosm.monitoring.adapters import TOOL_LABELS, HostCheckResult, MonitoringAdapter, make_adapter
from dosm.secrets import SecretNotFound, get_backend

router = APIRouter(prefix="/monitoring")

# ---------------------------------------------------------------------------
# In-process result cache  (hostname.lower(), source_id) -> (ts, result)
# ---------------------------------------------------------------------------

_result_cache: dict[tuple[str, int], tuple[float, HostCheckResult]] = {}
_CACHE_TTL = 60.0


def _cache_get(hostname: str, source_id: int) -> HostCheckResult | None:
    entry = _result_cache.get((hostname.lower(), source_id))
    if entry and (time.monotonic() - entry[0]) < _CACHE_TTL:
        return entry[1]
    return None


def _cache_put(hostname: str, result: HostCheckResult) -> None:
    _result_cache[(hostname.lower(), result.source_id)] = (time.monotonic(), result)


def _cache_clear_host(hostname: str) -> None:
    k = hostname.lower()
    for key in list(_result_cache):
        if key[0] == k:
            del _result_cache[key]

TOOL_CHOICES = ["dynatrace", "datadog", "servicenow", "prometheus"]
DD_SITES = ["datadoghq.com", "datadoghq.eu", "us3.datadoghq.com", "us5.datadoghq.com", "ap1.datadoghq.com", "ddog-gov.com"]


def _t(request: Request):
    return request.app.state.templates


# ---------------------------------------------------------------------------
# Search autocomplete
# ---------------------------------------------------------------------------

@router.get("/search")
async def search_hosts(
    q: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
) -> JSONResponse:
    if not q:
        return JSONResponse([])
    q_lower = q.lower()
    hosts = list_hosts(db)
    matches = [
        {"name": h.name, "hostname": h.hostname}
        for h in hosts
        if q_lower in h.name.lower() or q_lower in h.hostname.lower()
    ][:10]
    return JSONResponse(matches)


# ---------------------------------------------------------------------------
# Main monitoring page (search + results)
# ---------------------------------------------------------------------------

@router.get("", response_class=HTMLResponse)
async def monitoring_page(
    request: Request,
    hostname: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    results: list[HostCheckResult] | None = None
    error: str | None = None

    if hostname:
        sources = repo.list_enabled(db)
        if not sources:
            error = "No monitoring sources are configured yet. Add one under Manage Sources."
        else:
            cfg = request.app.state.config
            backend = get_backend(cfg)
            results = await _run_checks(hostname, sources, backend)

    return _t(request).TemplateResponse(
        request,
        "monitoring.html",
        {
            "user": user,
            "hostname": hostname,
            "results": results,
            "error": error,
            "has_sources": repo.has_any(db),
            "tool_labels": TOOL_LABELS,
        },
    )


# ---------------------------------------------------------------------------
# Sources list
# ---------------------------------------------------------------------------

@router.get("/sources", response_class=HTMLResponse)
async def list_sources(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    sources = repo.list_sources(db)
    return _t(request).TemplateResponse(
        request,
        "monitoring_sources.html",
        {"user": user, "sources": sources, "tool_labels": TOOL_LABELS},
    )


# ---------------------------------------------------------------------------
# New source form
# ---------------------------------------------------------------------------

@router.get("/sources/new", response_class=HTMLResponse)
async def new_source_form(
    request: Request,
    user: User = Depends(require_user),
) -> HTMLResponse:
    return _t(request).TemplateResponse(
        request,
        "monitoring_source_form.html",
        {
            "user": user,
            "source": None,
            "error": None,
            "tool_choices": TOOL_CHOICES,
            "dd_sites": DD_SITES,
            "tool_labels": TOOL_LABELS,
        },
    )


# ---------------------------------------------------------------------------
# Create source
# ---------------------------------------------------------------------------

@router.post("/sources", response_class=RedirectResponse)
async def create_source(
    request: Request,
    name: str = Form(...),
    tool: str = Form(...),
    url: str = Form(...),
    username: str = Form(""),
    token: str = Form(""),
    token2: str = Form(""),
    enabled: str | None = Form(None),
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
) -> RedirectResponse:
    if tool not in TOOL_CHOICES:
        raise HTTPException(status_code=400, detail=f"Unknown tool: {tool}")

    source = MonitoringSource(
        name=name.strip(),
        tool=tool,
        url=url.strip(),
        username=username.strip() or None,
        enabled=enabled is not None,
    )
    db.add(source)
    db.commit()
    db.refresh(source)

    cfg = request.app.state.config
    backend = get_backend(cfg)
    if token.strip():
        path = f"monitoring/{source.id}/token"
        backend.set_str(path, token.strip())
        source.token_secret = path
    if token2.strip():
        path2 = f"monitoring/{source.id}/token2"
        backend.set_str(path2, token2.strip())
        source.token2_secret = path2

    db.add(AuditLog(actor_id=user.id, action="monitoring_source.create", target=source.name))
    db.commit()
    return RedirectResponse("/monitoring/sources", status_code=303)


# ---------------------------------------------------------------------------
# Edit source form
# ---------------------------------------------------------------------------

@router.get("/sources/{source_id}/edit", response_class=HTMLResponse)
async def edit_source_form(
    source_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    source = repo.get_source(db, source_id)
    if source is None:
        raise HTTPException(status_code=404)
    return _t(request).TemplateResponse(
        request,
        "monitoring_source_form.html",
        {
            "user": user,
            "source": source,
            "error": None,
            "tool_choices": TOOL_CHOICES,
            "dd_sites": DD_SITES,
            "tool_labels": TOOL_LABELS,
        },
    )


# ---------------------------------------------------------------------------
# Update source
# ---------------------------------------------------------------------------

@router.post("/sources/{source_id}", response_class=RedirectResponse)
async def update_source(
    source_id: int,
    request: Request,
    name: str = Form(...),
    tool: str = Form(...),
    url: str = Form(...),
    username: str = Form(""),
    token: str = Form(""),
    token2: str = Form(""),
    enabled: str | None = Form(None),
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
) -> RedirectResponse:
    source = repo.get_source(db, source_id)
    if source is None:
        raise HTTPException(status_code=404)

    source.name = name.strip()
    source.tool = tool
    source.url = url.strip()
    source.username = username.strip() or None
    source.enabled = enabled is not None
    db.commit()

    cfg = request.app.state.config
    backend = get_backend(cfg)
    if token.strip():
        path = f"monitoring/{source_id}/token"
        backend.set_str(path, token.strip())
        source.token_secret = path
    if token2.strip():
        path2 = f"monitoring/{source_id}/token2"
        backend.set_str(path2, token2.strip())
        source.token2_secret = path2

    db.add(AuditLog(actor_id=user.id, action="monitoring_source.update", target=source.name))
    db.commit()
    return RedirectResponse("/monitoring/sources", status_code=303)


# ---------------------------------------------------------------------------
# Delete source
# ---------------------------------------------------------------------------

@router.post("/sources/{source_id}/delete", response_class=RedirectResponse)
async def delete_source(
    source_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
) -> RedirectResponse:
    source = repo.get_source(db, source_id)
    if source is None:
        raise HTTPException(status_code=404)

    name = source.name
    cfg = request.app.state.config
    backend = get_backend(cfg)
    for path in (source.token_secret, source.token2_secret):
        if path:
            try:
                backend.delete(path)
            except Exception:
                pass

    db.add(AuditLog(actor_id=user.id, action="monitoring_source.delete", target=name))
    db.delete(source)
    db.commit()
    return RedirectResponse("/monitoring/sources", status_code=303)


# ---------------------------------------------------------------------------
# Fleet coverage page
# ---------------------------------------------------------------------------

@router.get("/coverage", response_class=HTMLResponse)
async def coverage_page(
    request: Request,
    refresh: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    sources = repo.list_enabled(db)
    hosts = list_hosts(db)

    if refresh:
        for h in hosts:
            _cache_clear_host(h.hostname)

    rows: list[dict] = []
    col_found = [0] * len(sources)

    if hosts and sources:
        cfg = request.app.state.config
        backend = get_backend(cfg)
        matrix = await _run_checks_fleet(hosts, sources, backend)

        for host in hosts:
            cells = []
            for i, source in enumerate(sources):
                result = matrix.get((host.hostname.lower(), source.id))
                cells.append(result)
                if result and result.found:
                    col_found[i] += 1
            rows.append({"host": host, "cells": cells})

    n_sources = len(sources)
    fully = sum(
        1 for r in rows
        if n_sources > 0 and all(c and c.found for c in r["cells"])
    )
    partially = sum(
        1 for r in rows
        if n_sources > 0
        and any(c and c.found for c in r["cells"])
        and not all(c and c.found for c in r["cells"])
    )
    not_covered = len(rows) - fully - partially

    return _t(request).TemplateResponse(
        request,
        "monitoring_coverage.html",
        {
            "user": user,
            "sources": sources,
            "rows": rows,
            "tool_labels": TOOL_LABELS,
            "col_found": col_found,
            "n_hosts": len(hosts),
            "n_fully": fully,
            "n_partially": partially,
            "n_not_covered": not_covered,
            "has_sources": bool(sources),
            "has_hosts": bool(hosts),
        },
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _fleet_check_one(
    sem: asyncio.Semaphore,
    hostname: str,
    adapter: MonitoringAdapter,
    tool: str,
) -> HostCheckResult:
    cached = _cache_get(hostname, adapter.source_id)
    if cached:
        return cached
    async with sem:
        try:
            r = await adapter.check_host(hostname)
        except Exception as exc:
            r = HostCheckResult(
                source_id=adapter.source_id,
                source_name=adapter.source_name,
                tool=tool,
                found=False,
                error=str(exc),
            )
    _cache_put(hostname, r)
    return r


async def _run_checks_fleet(hosts, sources: list[MonitoringSource], backend) -> dict[tuple[str, int], HostCheckResult]:
    adapters: list[tuple[MonitoringAdapter, str]] = []
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
        if adapter:
            adapters.append((adapter, source.tool))

    if not adapters or not hosts:
        return {}

    sem = asyncio.Semaphore(20)
    tasks = [
        _fleet_check_one(sem, h.hostname, adapter, tool)
        for h in hosts
        for adapter, tool in adapters
    ]
    task_keys = [
        (h.hostname, adapter.source_id)
        for h in hosts
        for adapter, tool in adapters
    ]

    raw = await asyncio.gather(*tasks, return_exceptions=True)

    matrix: dict[tuple[str, int], HostCheckResult] = {}
    for (hostname, source_id), item in zip(task_keys, raw):
        if not isinstance(item, Exception):
            matrix[(hostname.lower(), source_id)] = item
    return matrix


async def _run_checks(hostname: str, sources: list[MonitoringSource], backend) -> list[HostCheckResult]:
    fresh_sources = []
    results: list[HostCheckResult] = []

    for source in sources:
        cached = _cache_get(hostname, source.id)
        if cached:
            results.append(cached)
        else:
            fresh_sources.append(source)

    if not fresh_sources:
        return results

    adapters: list[tuple[MonitoringAdapter, str]] = []
    for source in fresh_sources:
        try:
            token = backend.get_str(source.token_secret) if source.token_secret else ""
        except SecretNotFound:
            token = ""
        try:
            token2 = backend.get_str(source.token2_secret) if source.token2_secret else ""
        except SecretNotFound:
            token2 = ""
        adapter = make_adapter(source, token, token2)
        if adapter:
            adapters.append((adapter, source.tool))

    if not adapters:
        return results

    raw = await asyncio.gather(*(a.check_host(hostname) for a, _ in adapters), return_exceptions=True)
    for i, r in enumerate(raw):
        adapter, tool = adapters[i]
        if isinstance(r, Exception):
            r = HostCheckResult(
                source_id=adapter.source_id,
                source_name=adapter.source_name,
                tool=tool,
                found=False,
                error=str(r),
            )
        _cache_put(hostname, r)
        results.append(r)
    return results
