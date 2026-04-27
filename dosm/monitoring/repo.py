from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from dosm.models import MonitoringSource


def list_sources(db: Session) -> list[MonitoringSource]:
    return list(
        db.execute(
            select(MonitoringSource).order_by(MonitoringSource.tool, MonitoringSource.name)
        ).scalars()
    )


def list_enabled(db: Session) -> list[MonitoringSource]:
    return list(
        db.execute(
            select(MonitoringSource)
            .where(MonitoringSource.enabled.is_(True))
            .order_by(MonitoringSource.tool, MonitoringSource.name)
        ).scalars()
    )


def get_source(db: Session, source_id: int) -> MonitoringSource | None:
    return db.get(MonitoringSource, source_id)


def has_any(db: Session) -> bool:
    return db.execute(select(MonitoringSource).limit(1)).scalar_one_or_none() is not None
