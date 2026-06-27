from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from dosm.auth.tenancy import tenant_clause
from dosm.models import ConfluenceListener


def list_listeners(db: Session, tid: int | None) -> list[ConfluenceListener]:
    stmt = select(ConfluenceListener).order_by(ConfluenceListener.name)
    clause = tenant_clause(ConfluenceListener, tid)
    if clause is not None:
        stmt = stmt.where(clause)
    return list(db.execute(stmt).scalars())


def list_enabled(db: Session, tid: int | None = None) -> list[ConfluenceListener]:
    """Enabled listeners in tenant ``tid``. ``tid`` None yields every tenant's
    enabled listeners - used by the background poller, which runs cross-tenant."""
    stmt = (
        select(ConfluenceListener)
        .where(ConfluenceListener.enabled.is_(True))
        .order_by(ConfluenceListener.name)
    )
    clause = tenant_clause(ConfluenceListener, tid)
    if clause is not None:
        stmt = stmt.where(clause)
    return list(db.execute(stmt).scalars())


def get_listener(db: Session, listener_id: int, tid: int | None) -> ConfluenceListener | None:
    """Fetch by id, scoped to tenant ``tid``. Returns None when it belongs to a
    different tenant so callers 404 rather than leak existence."""
    row = db.get(ConfluenceListener, listener_id)
    if row is None:
        return None
    if tid is not None and row.tenant_id != tid:
        return None
    return row
