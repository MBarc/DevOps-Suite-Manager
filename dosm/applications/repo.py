"""CRUD + tree helpers for the 3-tier host organisation (OrgUnit).

Tier hierarchy is enforced here, not at the DB level (SQLite can't express the
"parent must be the tier directly above" rule). Trees are tiny (<= 3 deep), so
subtree walks issue a query per level rather than a recursive CTE.
"""
from __future__ import annotations

import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from dosm.auth.tenancy import tenant_clause
from dosm.models import Host, OrgUnit

# Ordered top -> bottom.
TIERS: tuple[str, ...] = ("application", "environment", "unit")
TIER_LABELS = {
    "application": "Application",
    "environment": "Environment",
    "unit": "Unit",
}
# tier -> the tier its parent must be (None => must be a root node).
PARENT_TIER: dict[str, str | None] = {
    "application": None,
    "environment": "application",
    "unit": "environment",
}
# tier -> the tier of its children (None => leaf tier).
CHILD_TIER: dict[str, str | None] = {
    "application": "environment",
    "environment": "unit",
    "unit": None,
}


class OrgValidationError(ValueError):
    pass


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return s or "item"


# ---- reads ----------------------------------------------------------------


def get_unit(db: Session, unit_id: int, tid: int | None) -> OrgUnit | None:
    """Fetch an org unit scoped to tenant ``tid`` (None = platform all-tenants)."""
    unit = db.get(OrgUnit, unit_id)
    if unit is None:
        return None
    if tid is not None and unit.tenant_id != tid:
        return None
    return unit


def list_units(db: Session, tid: int | None, *, tier: str | None = None) -> list[OrgUnit]:
    stmt = select(OrgUnit).order_by(OrgUnit.name)
    clause = tenant_clause(OrgUnit, tid)
    if clause is not None:
        stmt = stmt.where(clause)
    if tier is not None:
        stmt = stmt.where(OrgUnit.tier == tier)
    return list(db.execute(stmt).scalars())


def list_applications(db: Session, tid: int | None) -> list[OrgUnit]:
    """Root nodes (tier=application), eagerly loaded two levels deep."""
    stmt = (
        select(OrgUnit)
        .where(OrgUnit.parent_id.is_(None))
        .options(selectinload(OrgUnit.children).selectinload(OrgUnit.children))
        .order_by(OrgUnit.name)
    )
    clause = tenant_clause(OrgUnit, tid)
    if clause is not None:
        stmt = stmt.where(clause)
    return list(db.execute(stmt).scalars())


def children_of(db: Session, parent_id: int | None, tid: int | None) -> list[OrgUnit]:
    stmt = select(OrgUnit).where(OrgUnit.parent_id.is_(parent_id) if parent_id is None
                                 else OrgUnit.parent_id == parent_id).order_by(OrgUnit.name)
    # Root-level siblings (parent_id None) span tenants, so tenant-filter is
    # essential there; nested levels inherit the parent's tenant but we filter
    # anyway for defence in depth.
    clause = tenant_clause(OrgUnit, tid)
    if clause is not None:
        stmt = stmt.where(clause)
    return list(db.execute(stmt).scalars())


def get_by_path(db: Session, path: str, tid: int | None) -> OrgUnit | None:
    """Resolve an ``App/Env/Unit`` path (name match, case-insensitive) within
    tenant ``tid``."""
    parts = [p.strip() for p in path.split("/") if p.strip()]
    if not parts:
        return None
    parent_id: int | None = None
    node: OrgUnit | None = None
    tclause = tenant_clause(OrgUnit, tid)
    for part in parts:
        stmt = select(OrgUnit).where(func.lower(OrgUnit.name) == part.lower())
        stmt = stmt.where(
            OrgUnit.parent_id.is_(None) if parent_id is None
            else OrgUnit.parent_id == parent_id
        )
        if tclause is not None:
            stmt = stmt.where(tclause)
        node = db.execute(stmt).scalar_one_or_none()
        if node is None:
            return None
        parent_id = node.id
    return node


def subtree_ids(db: Session, unit: OrgUnit) -> list[int]:
    """All node ids in ``unit``'s subtree, including ``unit`` itself."""
    ids = [unit.id]
    frontier = [unit.id]
    while frontier:
        rows = db.execute(
            select(OrgUnit.id).where(OrgUnit.parent_id.in_(frontier))
        ).scalars().all()
        if not rows:
            break
        ids.extend(rows)
        frontier = rows
    return ids


def direct_host_counts(db: Session, tid: int | None) -> dict[int, int]:
    """Map of org_unit_id -> number of hosts assigned *directly* to that node,
    within tenant ``tid``."""
    stmt = (
        select(Host.org_unit_id, func.count(Host.id))
        .where(Host.org_unit_id.is_not(None))
        .group_by(Host.org_unit_id)
    )
    clause = tenant_clause(Host, tid)
    if clause is not None:
        stmt = stmt.where(clause)
    rows = db.execute(stmt).all()
    return {uid: n for uid, n in rows}


def build_tree(db: Session, tid: int | None) -> list[dict]:
    """Nested render structure: each node carries direct + rolled-up host counts.

    [{"unit": OrgUnit, "direct": int, "total": int, "children": [...]}, ...]
    """
    direct = direct_host_counts(db, tid)
    apps = list_applications(db, tid)

    def node(u: OrgUnit) -> dict:
        kids = [node(c) for c in sorted(u.children, key=lambda c: c.name.lower())]
        d = direct.get(u.id, 0)
        total = d + sum(k["total"] for k in kids)
        return {"unit": u, "direct": d, "total": total, "children": kids}

    return [node(a) for a in apps]


# ---- writes ---------------------------------------------------------------


def _unique_slug(db: Session, parent_id: int | None, base: str, tid: int,
                 exclude_id: int | None = None) -> str:
    siblings = children_of(db, parent_id, tid)
    taken = {s.slug for s in siblings if s.id != exclude_id}
    if base not in taken:
        return base
    i = 2
    while f"{base}-{i}" in taken:
        i += 1
    return f"{base}-{i}"


def _check_name_free(db: Session, parent_id: int | None, name: str, tid: int,
                     exclude_id: int | None = None) -> None:
    for s in children_of(db, parent_id, tid):
        if s.id != exclude_id and s.name.lower() == name.lower():
            raise OrgValidationError(
                f"A sibling named {name!r} already exists at this level."
            )


def create_unit(
    db: Session,
    *,
    tenant_id: int,
    name: str,
    tier: str,
    parent_id: int | None,
    description: str | None = None,
) -> OrgUnit:
    name = (name or "").strip()
    if not name:
        raise OrgValidationError("Name is required.")
    if tier not in TIERS:
        raise OrgValidationError(f"Unknown tier: {tier!r}")

    required_parent = PARENT_TIER[tier]
    if required_parent is None:
        if parent_id is not None:
            raise OrgValidationError("An application is a top-level node and has no parent.")
    else:
        if parent_id is None:
            raise OrgValidationError(
                f"A {TIER_LABELS[tier].lower()} must be placed under "
                f"a{'n' if required_parent[0] in 'aeiou' else ''} {required_parent}."
            )
        parent = get_unit(db, parent_id, tenant_id)
        if parent is None:
            raise OrgValidationError("Parent not found.")
        if parent.tier != required_parent:
            raise OrgValidationError(
                f"A {TIER_LABELS[tier].lower()} must be placed under "
                f"a{'n' if required_parent[0] in 'aeiou' else ''} {required_parent}, "
                f"not under a {parent.tier}."
            )

    _check_name_free(db, parent_id, name, tenant_id)
    slug = _unique_slug(db, parent_id, slugify(name), tenant_id)
    unit = OrgUnit(
        tenant_id=tenant_id,
        name=name, slug=slug, tier=tier, parent_id=parent_id,
        description=(description or None),
    )
    db.add(unit)
    db.flush()
    return unit


def update_unit(
    db: Session, unit: OrgUnit, *, name: str, description: str | None = None
) -> OrgUnit:
    """Rename + edit description. Tier and parent are immutable."""
    name = (name or "").strip()
    if not name:
        raise OrgValidationError("Name is required.")
    _check_name_free(db, unit.parent_id, name, unit.tenant_id, exclude_id=unit.id)
    if name.lower() != unit.name.lower():
        unit.slug = _unique_slug(
            db, unit.parent_id, slugify(name), unit.tenant_id, exclude_id=unit.id
        )
    unit.name = name
    unit.description = description or None
    db.flush()
    return unit


def delete_unit(db: Session, unit: OrgUnit) -> None:
    """Delete a node and its subtree (ORM cascade). Hosts pointing at any
    removed node are SET NULL by the FK on create_all."""
    db.delete(unit)
    db.flush()


def assign_host(db: Session, host: Host, org_unit_id: int | None) -> None:
    """Assign ``host`` to an org unit in the *same tenant* (or clear it)."""
    if org_unit_id is not None:
        unit = db.get(OrgUnit, org_unit_id)
        if unit is None or unit.tenant_id != host.tenant_id:
            raise OrgValidationError("Org unit not found.")
    host.org_unit_id = org_unit_id
    db.flush()
