"""Tenant scoping - the heart of multi-tenancy.

Every operational row belongs to exactly one tenant (``Model.tenant_id``).
Regular users (viewer/operator/admin) are confined to their own
``User.tenant_id``. ``platform_admin`` users are tenant-less and pick an
**active tenant** via a switcher stored in the session; with no active tenant
selected they get a read-only "All tenants" overview.

This module centralises three things so route/repo code stays uniform:

- ``active_tenant_id`` - the FastAPI dependency that resolves the tenant a
  request operates in (or ``None`` = the platform-admin all-tenants view).
- ``require_active_tenant`` - the mutation gate: a platform admin must have an
  active tenant selected before creating/editing tenant-scoped rows.
- ``tenant_clause`` - the SQLAlchemy filter helper; ``None`` means "no filter"
  (the all-tenants read path), mirroring how ``visible_credentials_filter`` in
  ``dosm/credentials/access.py`` returns an optional clause.
"""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status

from dosm.auth.deps import is_platform_admin, require_user
from dosm.models import User

# Session key holding a platform admin's chosen active tenant id.
ACTIVE_TENANT_SESSION_KEY = "active_tenant_id"


def resolve_tenant_id(request: Request, user: User) -> int | None:
    """Plain (non-Depends) resolver, usable from WebSocket handlers.

    - Non-platform users are always pinned to their own ``tenant_id``.
    - Platform admins use the session's active tenant; ``None`` means the
      all-tenants overview (read-only).
    """
    if not is_platform_admin(user):
        return user.tenant_id
    raw = request.session.get(ACTIVE_TENANT_SESSION_KEY)
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def active_tenant_id(
    request: Request,
    user: User = Depends(require_user),
) -> int | None:
    """FastAPI dependency: the tenant id this request reads/writes within.

    ``None`` only ever happens for a platform admin who has not selected an
    active tenant (the all-tenants read view). Regular users always get a
    concrete id.
    """
    return resolve_tenant_id(request, user)


def require_active_tenant(
    tid: int | None = Depends(active_tenant_id),
) -> int:
    """Mutation gate: returns a concrete tenant id or 403.

    Use on create/edit/delete routes. A regular user always passes (their
    tenant id). A platform admin must have an active tenant selected - writing
    into "all tenants" is undefined, so we refuse.
    """
    if tid is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="select an active tenant before making changes",
        )
    return tid


def tenant_clause(model, tid: int | None):
    """Return ``model.tenant_id == tid`` or ``None`` (no filter).

    ``None`` is the platform-admin all-tenants read path. Callers apply the
    clause only when it is not None::

        clause = tenant_clause(Host, tid)
        if clause is not None:
            stmt = stmt.where(clause)
    """
    if tid is None:
        return None
    return model.tenant_id == tid
