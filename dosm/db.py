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
    engine = create_engine(url, future=True, connect_args={"check_same_thread": False})
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
    """Create all tables. Used by `dosm db init`. Safe to re-run."""
    engine = init_engine(cfg)
    Base.metadata.create_all(engine)
