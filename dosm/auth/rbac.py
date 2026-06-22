"""Group -> (tenant, role) mapping store (Phase 24b).

The mapping that gates SSO access moved from ``config.yaml``
(``rbac.group_role_map``, single-tenant) into the ``group_mappings`` DB table so
a group can grant a role *within a specific tenant*. These helpers are the
single place that reads/writes that table; the Okta callback resolves grants via
``dosm.auth.okta.resolve_grant``.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from dosm.db import session_scope
from dosm.models import GroupMapping, Tenant


def seed_group_mappings_from_config(cfg) -> int:
    """One-time upgrade: copy ``config.yaml`` ``rbac.group_role_map`` into the
    ``group_mappings`` table (under the Default tenant) when the table is still
    empty. Idempotent - a no-op once any mapping exists. Returns rows seeded."""
    grm = dict(cfg.rbac.group_role_map or {})
    if not grm:
        return 0
    with session_scope() as s:
        if s.execute(select(GroupMapping.id).limit(1)).first() is not None:
            return 0
        default_tid = s.execute(
            select(Tenant.id).where(Tenant.slug == "default")
        ).scalar_one_or_none()
        if default_tid is None:
            return 0
        n = 0
        for group, role in grm.items():
            s.add(GroupMapping(group_name=group, tenant_id=int(default_tid), role=role))
            n += 1
        return n


def list_mappings(db: Session, tid: int | None) -> list[GroupMapping]:
    """Group mappings visible to the caller. ``tid`` None = the platform-admin
    view (every mapping, including tenant-less platform_admin grants). A concrete
    ``tid`` returns only that tenant's grants (NULL-tenant platform grants are
    excluded, so tenant admins never see them). Ordered by group name."""
    stmt = select(GroupMapping).order_by(GroupMapping.group_name)
    if tid is not None:
        stmt = stmt.where(GroupMapping.tenant_id == tid)
    return list(db.execute(stmt).scalars())


def get_by_id(db: Session, mapping_id: int) -> GroupMapping | None:
    return db.get(GroupMapping, mapping_id)


def get_mapping(db: Session, group: str, tid: int | None) -> GroupMapping | None:
    # ``tid`` None matches the tenant-less platform_admin grant (IS NULL).
    return db.execute(
        select(GroupMapping).where(
            GroupMapping.group_name == group, GroupMapping.tenant_id == tid
        )
    ).scalar_one_or_none()


def upsert_mapping(db: Session, group: str, tid: int | None, role: str) -> bool:
    """Add or update a group -> role grant. ``tid`` None creates a tenant-less
    platform_admin grant. Returns True if an existing row was updated."""
    existing = get_mapping(db, group, tid)
    if existing is not None:
        existing.role = role
        db.flush()
        return True
    db.add(GroupMapping(group_name=group, tenant_id=tid, role=role))
    db.flush()
    return False


def delete_by_id(db: Session, mapping: GroupMapping) -> None:
    db.delete(mapping)
    db.flush()
