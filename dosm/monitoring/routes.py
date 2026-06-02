from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from dosm.auth.deps import require_user
from dosm.db import get_session
from dosm.hosts.repo import list_hosts
from dosm.models import AuditLog, MonitoringMatch, MonitoringSource, Tag, User
from dosm.monitoring import repo
from dosm.monitoring.adapters import TOOL_LABELS, HostCheckResult, MonitoringAdapter, make_adapter
from dosm.secrets import SecretNotFound, get_backend

router = APIRouter(prefix="/monitoring")

# ---------------------------------------------------------------------------
# Persistent host-check cache (monitoring_matches table). A found/known entry
# is served locally until it ages past the TTL; stale/missing entries trigger a
# fresh API query, and a manual Refresh forces a re-query. Presence/identity
# only — live alert state (fetch_alerts) is never cached here.
# ---------------------------------------------------------------------------

_MATCH_TTL = timedelta(hours=24)


def _match_fresh(m: MonitoringMatch | None) -> bool:
    if m is None:
        return False
    ts = m.checked_at if m.checked_at.tzinfo else m.checked_at.replace(tzinfo=UTC)
    return (datetime.now(UTC) - ts) < _MATCH_TTL


def _match_to_result(source: MonitoringSource, m: MonitoringMatch) -> HostCheckResult:
    return HostCheckResult(
        source_id=source.id, source_name=source.name, tool=source.tool,
        found=m.found, entity_id=m.entity_id, entity_name=m.entity_name,
        entity_url=m.entity_url, extra=json.loads(m.extra_json or "{}"), error=m.error,
    )


def _match_store(db: Session, hostname: str, r: HostCheckResult) -> None:
    key = hostname.lower()
    m = db.execute(
        select(MonitoringMatch).where(
            MonitoringMatch.hostname == key, MonitoringMatch.source_id == r.source_id
        )
    ).scalar_one_or_none()
    if m is None:
        m = MonitoringMatch(hostname=key, source_id=r.source_id)
        db.add(m)
    m.found = r.found
    m.entity_id = r.entity_id
    m.entity_name = r.entity_name
    m.entity_url = r.entity_url
    m.extra_json = json.dumps(r.extra or {})
    m.error = r.error
    m.checked_at = datetime.now(UTC)


def _matches_for(db: Session, hostnames: list[str]) -> dict[tuple[str, int], MonitoringMatch]:
    keys = [h.lower() for h in hostnames]
    if not keys:
        return {}
    rows = db.execute(
        select(MonitoringMatch).where(MonitoringMatch.hostname.in_(keys))
    ).scalars()
    return {(m.hostname, m.source_id): m for m in rows}


def _match_clear_host(db: Session, hostname: str) -> None:
    db.execute(delete(MonitoringMatch).where(MonitoringMatch.hostname == hostname.lower()))

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
    refresh: str = "",
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
            results = await _run_checks(db, hostname, sources, backend, force=bool(refresh))

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

def _glob_to_regex(pattern: str) -> re.Pattern:
    """Convert a glob-ish pattern to a compiled regex.

    If the pattern already contains regex metacharacters (^, $, (, [, .)
    it is used as-is; otherwise * and ? are converted to .* and . respectively
    and the whole thing is anchored with ^ and $.
    """
    if re.search(r"[\^\$\(\[\.]", pattern):
        return re.compile(pattern, re.IGNORECASE)
    escaped = re.escape(pattern).replace(r"\*", ".*").replace(r"\?", ".")
    return re.compile(f"^{escaped}$", re.IGNORECASE)


def _filter_hosts(hosts, pattern: str, tag_names: list[str]):
    """Return the subset of hosts matching *both* pattern and tag filters."""
    filtered = hosts
    if pattern:
        rx = _glob_to_regex(pattern)
        filtered = [h for h in filtered if rx.search(h.name) or rx.search(h.hostname)]
    if tag_names:
        tag_set = {t.lower() for t in tag_names}
        filtered = [h for h in filtered if any(t.name.lower() in tag_set for t in h.tags)]
    return filtered


@router.get("/coverage", response_class=HTMLResponse)
async def coverage_page(
    request: Request,
    refresh: str = "",
    pattern: str = "",
    tags: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    sources = repo.list_enabled(db)
    all_hosts = list_hosts(db)
    all_tags = list(db.execute(select(Tag).order_by(Tag.name)).scalars())

    selected_tags = [t for t in tags.split(",") if t.strip()] if tags else []
    hosts = _filter_hosts(all_hosts, pattern.strip(), selected_tags)

    rows: list[dict] = []
    col_found = [0] * len(sources)

    if hosts and sources:
        cfg = request.app.state.config
        backend = get_backend(cfg)
        matrix = await _run_checks_fleet(db, hosts, sources, backend, force=bool(refresh))

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
            "n_hosts": len(all_hosts),
            "n_filtered": len(hosts),
            "n_fully": fully,
            "n_partially": partially,
            "n_not_covered": not_covered,
            "has_sources": bool(sources),
            "has_hosts": bool(all_hosts),
            "all_tags": all_tags,
            "filter_pattern": pattern.strip(),
            "filter_tags": selected_tags,
        },
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _adapters_for(sources: list[MonitoringSource], backend) -> list[tuple[MonitoringAdapter, MonitoringSource]]:
    out: list[tuple[MonitoringAdapter, MonitoringSource]] = []
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
            out.append((adapter, source))
    return out


def _error_result(adapter: MonitoringAdapter, source: MonitoringSource, exc: BaseException) -> HostCheckResult:
    return HostCheckResult(
        source_id=adapter.source_id, source_name=adapter.source_name,
        tool=source.tool, found=False, error=str(exc),
    )


async def _run_checks(
    db: Session, hostname: str, sources: list[MonitoringSource], backend, *, force: bool = False
) -> list[HostCheckResult]:
    results: list[HostCheckResult] = []
    matches = _matches_for(db, [hostname])
    fresh_sources: list[MonitoringSource] = []
    for source in sources:
        m = matches.get((hostname.lower(), source.id))
        if not force and _match_fresh(m):
            results.append(_match_to_result(source, m))
        else:
            fresh_sources.append(source)

    adapters = _adapters_for(fresh_sources, backend)
    if not adapters:
        return results

    raw = await asyncio.gather(*(a.check_host(hostname) for a, _ in adapters), return_exceptions=True)
    for (adapter, source), r in zip(adapters, raw):
        if isinstance(r, Exception):
            r = _error_result(adapter, source, r)
        _match_store(db, hostname, r)   # DB write outside the gather — single session
        results.append(r)
    db.commit()
    return results


async def _run_checks_fleet(
    db: Session, hosts, sources: list[MonitoringSource], backend, *, force: bool = False
) -> dict[tuple[str, int], HostCheckResult]:
    adapters = _adapters_for(sources, backend)
    if not adapters or not hosts:
        return {}

    matches = _matches_for(db, [h.hostname for h in hosts])
    matrix: dict[tuple[str, int], HostCheckResult] = {}
    to_check: list[tuple[object, MonitoringAdapter, MonitoringSource]] = []
    for h in hosts:
        for adapter, source in adapters:
            m = matches.get((h.hostname.lower(), source.id))
            if not force and _match_fresh(m):
                matrix[(h.hostname.lower(), source.id)] = _match_to_result(source, m)
            else:
                to_check.append((h, adapter, source))

    if to_check:
        sem = asyncio.Semaphore(20)

        async def _check(adapter, hostname):
            async with sem:
                return await adapter.check_host(hostname)

        raw = await asyncio.gather(
            *(_check(a, h.hostname) for h, a, _ in to_check), return_exceptions=True
        )
        # DB writes happen here, sequentially — never inside the gather above.
        for (h, adapter, source), r in zip(to_check, raw):
            if isinstance(r, Exception):
                r = _error_result(adapter, source, r)
            _match_store(db, h.hostname, r)
            matrix[(h.hostname.lower(), source.id)] = r
        db.commit()
    return matrix
