from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from dosm.auth.tenancy import tenant_clause
from dosm.models import MonitoringSource


def list_sources(db: Session, tid: int | None) -> list[MonitoringSource]:
    stmt = select(MonitoringSource).order_by(MonitoringSource.tool, MonitoringSource.name)
    clause = tenant_clause(MonitoringSource, tid)
    if clause is not None:
        stmt = stmt.where(clause)
    return list(db.execute(stmt).scalars())


def list_enabled(db: Session, tid: int | None) -> list[MonitoringSource]:
    """Enabled sources in tenant ``tid``. ``tid`` None yields every tenant's
    enabled sources - used by the background poller, which runs cross-tenant."""
    stmt = (
        select(MonitoringSource)
        .where(MonitoringSource.enabled.is_(True))
        .order_by(MonitoringSource.tool, MonitoringSource.name)
    )
    clause = tenant_clause(MonitoringSource, tid)
    if clause is not None:
        stmt = stmt.where(clause)
    return list(db.execute(stmt).scalars())


def get_source(db: Session, source_id: int, tid: int | None) -> MonitoringSource | None:
    """Fetch a source by id, scoped to tenant ``tid``. Returns None when it
    belongs to a different tenant so callers 404 rather than leak existence."""
    source = db.get(MonitoringSource, source_id)
    if source is None:
        return None
    if tid is not None and source.tenant_id != tid:
        return None
    return source


def has_any(db: Session, tid: int | None) -> bool:
    stmt = select(MonitoringSource).limit(1)
    clause = tenant_clause(MonitoringSource, tid)
    if clause is not None:
        stmt = stmt.where(clause)
    return db.execute(stmt).scalar_one_or_none() is not None
