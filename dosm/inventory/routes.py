from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from dosm.applications import repo as org_repo
from dosm.auth.deps import require_user
from dosm.auth.tenancy import active_tenant_id
from dosm.credentials.access import (
    visible_credentials_filter,
    visible_credentials_query,
)
from dosm.db import get_session
from dosm.hosts import repo as hosts_repo
from dosm.models import Credential, Host, Pipeline, User
from dosm.pipelines import repo as pipe_repo
from dosm.pipelines.access import visible_pipelines_filter
from dosm.pipelines.adapters import get_adapter

router = APIRouter(prefix="/inventory")


def _merge(*dicts) -> dict[int, int]:
    out: dict[int, int] = {}
    for d in dicts:
        for k, v in d.items():
            out[k] = out.get(k, 0) + v
    return out


@router.get("", response_class=HTMLResponse, include_in_schema=False)
async def inventory(
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
    tid: int | None = Depends(active_tenant_id),
):
    """One explorer over hosts + pipelines + credentials, filed into the shared
    org tree. Everything is sent client-side; the type-filter pills + folder
    tree + search do the filtering."""
    hosts = hosts_repo.list_hosts(db, tid=tid)
    creds = list(db.execute(visible_credentials_query(user, tid)).scalars())
    pipelines = pipe_repo.list_pipelines(db, tid, user)
    pipe_rows = []
    for p in pipelines:
        try:
            adapter = get_adapter(p.provider)
            provider_name = adapter.display_name or p.provider
            summary = adapter.target_summary(json.loads(p.config or "{}"))
        except Exception:
            provider_name, summary = p.provider, ""
        latest = pipe_repo.list_runs(db, p.id, limit=1)
        pipe_rows.append({"p": p, "provider_name": provider_name, "summary": summary,
                          "latest": latest[0] if latest else None})

    pv = visible_pipelines_filter(user)
    cv = visible_credentials_filter(user)
    counts = _merge(
        org_repo.direct_counts(db, tid, Host),
        org_repo.direct_counts(db, tid, Pipeline, extra=None if pv is True else pv),
        org_repo.direct_counts(db, tid, Credential, extra=None if cv is True else cv),
    )
    tree = org_repo.build_tree(db, tid, counts=counts)

    n_total = len(hosts) + len(pipe_rows) + len(creds)
    n_unassigned = (
        sum(1 for h in hosts if h.org_unit_id is None)
        + sum(1 for r in pipe_rows if r["p"].org_unit_id is None)
        + sum(1 for c in creds if c.org_unit_id is None)
    )
    return request.app.state.templates.TemplateResponse(
        request, "inventory/explorer.html", {
            "hosts": hosts, "pipelines": pipe_rows, "credentials": creds,
            "tree": tree, "n_total": n_total, "n_unassigned": n_unassigned,
            "initial_org_unit_id": None, "user": user,
            "guacamole_enabled": request.app.state.config.guacamole.enabled,
        })
