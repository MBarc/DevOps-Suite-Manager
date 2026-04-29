"""Department sync orchestrator.

One function: ``sync_department(db, cfg, dept, *, actor_id)`` runs a single
group sync against the configured AD source, applies the diff to the DB,
infers the parent department from the returned manager chain, and writes an
audit entry.

Kept synchronous because the FastAPI route dispatches it to a threadpool —
SQLAlchemy and pywinrm are both blocking, so a sync function avoids the
``run_in_executor`` boilerplate at every call site.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from dosm.config import Config
from dosm.directory import (
    AdDirectoryError,
    AdDirectoryUnreachable,
    AdGroupNotFound,
    AdUserNotFound,
    get_directory_source,
)
from dosm.models import AuditLog, Department, DepartmentMember


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def resolve_inputs(cfg: Config, group_name: str, manager_input: str) -> tuple[str, str, dict]:
    """Validate user-entered group + manager strings against AD.

    Returns ``(group_dn, manager_dn, manager_attrs)``. Raises
    ``AdGroupNotFound`` / ``AdUserNotFound`` for the user-facing cases the
    form needs to surface as inline errors.
    """
    src = get_directory_source(cfg)
    group = src.resolve_group(group_name)
    manager = src.resolve_user(manager_input)
    return (
        group.distinguished_name,
        manager.distinguished_name,
        {
            "name": manager.display_name,
            "email": manager.email,
            "title": manager.title,
        },
    )


def _diff_members(
    existing: list[DepartmentMember],
    incoming_dns: set[str],
) -> tuple[list[DepartmentMember], list[str]]:
    """Return (rows_to_delete, dns_currently_kept_or_added)."""
    to_delete = [m for m in existing if m.user_dn not in incoming_dns]
    return to_delete, list(incoming_dns)


def _infer_parent_id(
    db: Session, current_dept_id: int | None, manager_chain: list[str]
) -> int | None:
    """Walk the chain and return the first matching dept's id (excluding self)."""
    if not manager_chain:
        return None
    rows = db.execute(
        select(Department.id, Department.manager_dn).where(
            Department.manager_dn.in_(manager_chain)
        )
    ).all()
    by_dn = {dn: did for did, dn in rows if dn}
    for dn in manager_chain:
        if dn in by_dn and by_dn[dn] != current_dept_id:
            return by_dn[dn]
    return None


def sync_department(
    db: Session, cfg: Config, dept: Department, *, actor_id: int | None
) -> dict:
    """Run a sync for one department. Commits its own transaction. Returns
    a small summary dict for the route to flash to the user.

    The caller has already created the dept row (with ad_group_dn and
    manager_dn populated by ``resolve_inputs`` at form submit time). This
    function only needs to refresh members, manager attrs, and parent_id.

    On AD failure: writes ``last_sync_error``, sets ``sync_status='error'``,
    and re-raises so the route can show a banner. Cached members from a
    prior successful sync are left intact.
    """
    db.add(
        AuditLog(actor_id=actor_id, action="org.sync.start", target=f"dept:{dept.slug}")
    )
    db.flush()

    if not dept.ad_group_dn or not dept.manager_dn:
        # Should not happen — form validation populates both — but guard
        # against a stale row from before this feature.
        raise AdDirectoryError(
            f"department {dept.slug!r} is missing ad_group_dn or manager_dn"
        )

    src = get_directory_source(cfg)
    try:
        result = src.sync_group(dept.ad_group_dn, dept.manager_dn)
    except (AdDirectoryUnreachable, AdGroupNotFound, AdUserNotFound) as e:
        dept.last_sync_error = str(e)
        dept.sync_status = "error"
        db.add(
            AuditLog(
                actor_id=actor_id,
                action="org.sync.fail",
                target=f"dept:{dept.slug}",
                details=str(e)[:500],
            )
        )
        db.commit()
        raise

    # ---- Apply group + manager attrs ------------------------------------
    dept.ad_group_name = result.group.name
    dept.ad_group_dn = result.group.distinguished_name
    if result.manager is not None:
        dept.manager_dn = result.manager.distinguished_name
        dept.manager_name = result.manager.display_name
        dept.manager_email = result.manager.email
        dept.manager_title = result.manager.title

    # ---- Apply membership diff ------------------------------------------
    existing = list(
        db.execute(
            select(DepartmentMember).where(DepartmentMember.department_id == dept.id)
        ).scalars()
    )
    by_dn = {m.user_dn: m for m in existing}
    incoming_dns = {m.user_dn for m in result.members}
    added: list[str] = []
    removed: list[str] = []

    for m in result.members:
        row = by_dn.get(m.user_dn)
        if row is None:
            db.add(
                DepartmentMember(
                    department_id=dept.id,
                    user_dn=m.user_dn,
                    display_name=m.display_name,
                    email=m.email,
                    title=m.title,
                    phone=m.phone,
                    enabled=m.enabled,
                    manager_dn=m.manager_dn,
                    manager_name=m.manager_name,
                    synced_at=_utcnow(),
                )
            )
            added.append(m.user_dn)
        else:
            row.display_name = m.display_name
            row.email = m.email
            row.title = m.title
            row.phone = m.phone
            row.enabled = m.enabled
            row.manager_dn = m.manager_dn
            row.manager_name = m.manager_name
            row.synced_at = _utcnow()

    for row in existing:
        if row.user_dn not in incoming_dns:
            db.delete(row)
            removed.append(row.user_dn)

    # ---- Hierarchy inference --------------------------------------------
    new_parent_id = _infer_parent_id(db, dept.id, result.manager_chain)
    parent_changed = new_parent_id != dept.parent_id
    dept.parent_id = new_parent_id

    # ---- Finalize -------------------------------------------------------
    dept.last_synced_at = _utcnow()
    dept.last_sync_error = None
    dept.sync_status = "ok"

    summary = {
        "added": len(added),
        "removed": len(removed),
        "kept": len(incoming_dns) - len(added),
        "parent_changed": parent_changed,
        "parent_id": new_parent_id,
        "manager_chain_depth": len(result.manager_chain),
    }
    db.add(
        AuditLog(
            actor_id=actor_id,
            action="org.sync.complete",
            target=f"dept:{dept.slug}",
            details=json.dumps(summary),
        )
    )
    db.commit()
    return summary
