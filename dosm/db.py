from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from dosm.config import Config, get_config
from dosm.models import Base

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def make_engine(cfg: Config) -> Engine:
    _ensure_parent(cfg.db_path)
    url = f"sqlite:///{cfg.db_path}"
    engine = create_engine(
        url,
        future=True,
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    # WAL gives concurrent readers + one writer without "database is locked"
    # errors when the secrets backend opens a session inside a request.
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        try:
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA foreign_keys=ON")
        finally:
            cur.close()

    return engine


def init_engine(cfg: Config | None = None) -> Engine:
    """Idempotently initialize the process-wide engine + session factory."""
    global _engine, _SessionLocal
    if _engine is not None:
        return _engine
    cfg = cfg or get_config()
    _engine = make_engine(cfg)
    _SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False, future=True)
    return _engine


def get_engine() -> Engine:
    if _engine is None:
        init_engine()
    assert _engine is not None
    return _engine


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional session context for CLI / background tasks."""
    if _SessionLocal is None:
        init_engine()
    assert _SessionLocal is not None
    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency: yields a session, commits on success."""
    if _SessionLocal is None:
        init_engine()
    assert _SessionLocal is not None
    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def create_all(cfg: Config | None = None) -> None:
    """Create all tables and apply idempotent column-add migrations.

    Safe to re-run; used both by `dosm db init` and by `init_engine` so a
    DOSM_HOME from an earlier phase upgrades transparently.
    """
    from dosm.migrations import run_migrations

    engine = init_engine(cfg)
    Base.metadata.create_all(engine)
    run_migrations(engine)
