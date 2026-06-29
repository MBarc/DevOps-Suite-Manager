"""Credential visibility helpers (RBAC private vs shared).

A credential is either ``shared`` (visible to everyone) or ``private`` (visible
only to its ``owner_id`` and to admins). These helpers are the single source of
truth for "can this user see / use this credential" and are reused by the
credentials routes, the host-form credential picker, and every use-time
resolution path (Guacamole / FTP / metrics / jump chains).

Keeping the rule in one place avoids each call site re-deriving the predicate
and drifting out of sync.
"""
from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.sql import Select

from dosm.auth.deps import user_has_role
from dosm.auth.tenancy import tenant_clause
from dosm.models import Credential, User


def _is_admin(user: User | None) -> bool:
    # tenant_admin OR platform_admin - both get the unrestricted visibility view
    # (still tenant-scoped separately via ``tenant_clause``).
    return user_has_role(user, "tenant_admin")


def can_see_credential(user: User | None, cred: Credential) -> bool:
    """True if ``user`` may see ``cred`` (list it, open its detail page)."""
    if cred.visibility != "private":
        return True
    if _is_admin(user):
        return True
    return user is not None and cred.owner_id == user.id


def can_use_credential(user: User | None, cred: Credential) -> bool:
    """True if ``user`` may use ``cred`` to open a connection.

    A tenant-scoped user may only use credentials from their own tenant; a
    platform admin (``tenant_id is None``) is unrestricted. This tenant check is
    the use-time backstop against a host pinned - via a forged id - to another
    tenant's credential, layered on the private/shared visibility rule."""
    if user is not None and user.tenant_id is not None and cred.tenant_id != user.tenant_id:
        return False
    return can_see_credential(user, cred)


def visible_credentials_filter(user: User | None):
    """A SQLAlchemy boolean clause restricting ``Credential`` rows to those
    ``user`` may see. Use inside ``select(Credential).where(...)``."""
    if _is_admin(user):
        return True  # no restriction
    owner_id = user.id if user is not None else None
    return or_(
        Credential.visibility != "private",
        Credential.owner_id == owner_id,
    )


def visible_credentials_query(user: User | None, tid: int | None) -> Select:
    """A ready ``select(Credential)`` filtered to what ``user`` may see within
    tenant ``tid``, ordered by name. ``tid`` is the active tenant id (None =
    platform-admin all-tenants view, no tenant restriction)."""
    stmt = select(Credential)
    tclause = tenant_clause(Credential, tid)
    if tclause is not None:
        stmt = stmt.where(tclause)
    vclause = visible_credentials_filter(user)
    if vclause is not True:
        stmt = stmt.where(vclause)
    return stmt.order_by(Credential.name)


def visible_credentials(db, user: User | None, tid: int | None) -> list[Credential]:
    """List of ``Credential`` rows ``user`` may see in tenant ``tid`` (for
    picker dropdowns)."""
    return list(db.execute(visible_credentials_query(user, tid)).scalars())


def first_unusable_credential(user: User | None, creds) -> Credential | None:
    """Return the first credential in ``creds`` that ``user`` may *not* use, or
    ``None`` if all are usable. ``None`` entries are skipped. Use at connection
    time to block a user from connecting via a private credential they don't own
    (e.g. a shared host pinned to someone else's private credential)."""
    for cred in creds:
        if cred is not None and not can_use_credential(user, cred):
            return cred
    return None


def first_unprovisioned_dynamic(cfg, user: User | None, creds) -> Credential | None:
    """Return the first *dynamic* (per-user/PIM) credential in ``creds`` that
    ``user`` has not stored their own secret for, or ``None``. Use at connection
    time so we can tell the user to set up their credentials before connecting,
    instead of failing mid-connect."""
    from dosm.credentials.dynamic import has_user_material, is_dynamic

    uid = user.id if user is not None else None
    for cred in creds:
        if cred is not None and is_dynamic(cred) and not has_user_material(cfg, cred, uid):
            return cred
    return None
