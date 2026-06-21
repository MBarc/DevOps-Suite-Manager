from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

import dosm.db as _db_module
from dosm.auth.passwords import hash_password
from dosm.config import Config, DocsIndexConfig, PipelinesConfig, RecordingConfig
from dosm.models import (
    AuditLog,
    Base,
    CertSource,
    ChatMessage,
    Conversation,
    Credential,
    Department,
    DepartmentMember,
    DocChunk,
    Document,
    Folder,
    Host,
    HostTag,
    MonitoringSource,
    NetworkScan,
    OrgUnit,
    Pipeline,
    PipelinePayload,
    PipelineRun,
    PlanCard,
    RecordingSession,
    Tag,
    Tenant,
    User,
)


@pytest.fixture(scope="session")
def test_home(tmp_path_factory):
    home = tmp_path_factory.mktemp("dosm_home")
    (home / "config").mkdir()
    (home / "data").mkdir()
    (home / "data" / "recording_tmp").mkdir()
    (home / "docs").mkdir()
    (home / "docs" / "sessions").mkdir()
    (home / "config.yaml").write_text("")
    return home


@pytest.fixture(scope="session")
def test_config(test_home):
    return Config(
        home=test_home,
        docs_index=DocsIndexConfig(auto_index_on_startup=False, embedder="none"),
        pipelines=PipelinesConfig(poller_enabled=False),
        recording=RecordingConfig(enabled=False),
    )


@pytest.fixture(scope="session")
def db_engine(test_config):
    engine = create_engine(
        f"sqlite:///{test_config.db_path}",
        connect_args={"check_same_thread": False},
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _pragma(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(engine)
    from dosm.migrations import run_migrations
    run_migrations(engine)
    return engine


@pytest.fixture(scope="session")
def session_factory(db_engine):
    return sessionmaker(bind=db_engine, autoflush=False, autocommit=False, future=True)


@pytest.fixture(scope="session")
def app(test_config, db_engine, session_factory):
    # Patch the process-wide engine globals before create_app runs so every
    # route's get_session() uses the test database, not the real one.
    _db_module._engine = db_engine
    _db_module._SessionLocal = session_factory

    from dosm.main import create_app
    return create_app(test_config)


@pytest.fixture(scope="session")
def default_tenant(session_factory):
    """The Default tenant created by ``run_migrations`` - the home of the test
    admin and the tenant most single-tenant tests operate within."""
    with session_factory() as s:
        t = s.execute(
            text("SELECT id FROM tenants WHERE slug = 'default'")
        ).scalar_one()
        return {"id": int(t), "slug": "default"}


@pytest.fixture(scope="session")
def admin_user(session_factory, default_tenant):
    with session_factory() as s:
        user = User(
            username="testadmin",
            password_hash=hash_password("testpass"),
            role="admin",
            tenant_id=default_tenant["id"],
            is_active=True,
        )
        s.add(user)
        s.commit()
        s.refresh(user)
        return {"id": user.id, "username": user.username, "tenant_id": user.tenant_id}


@pytest.fixture(autouse=True)
def clean_tables(session_factory, admin_user, default_tenant):
    """Wipe all rows after every test, preserving the test admin and the
    Default tenant (and any other session-scoped tenants/users created by
    isolation-test fixtures, which manage their own teardown)."""
    yield
    with session_factory() as s:
        for model in [
            PlanCard,
            ChatMessage,
            Conversation,
            DocChunk,
            Document,
            Folder,
            PipelinePayload,
            PipelineRun,
            Pipeline,
            AuditLog,
            HostTag,
            Host,
            Credential,
            CertSource,
            DepartmentMember,
            Department,
            OrgUnit,
            NetworkScan,
            MonitoringSource,
            RecordingSession,
            Tag,
        ]:
            s.execute(text(f"DELETE FROM {model.__tablename__}"))
        s.commit()


@pytest.fixture
def db(session_factory):
    """Fresh session for direct DB reads/writes inside a test."""
    s = session_factory()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


@pytest.fixture
def anon_client(app):
    """TestClient with no session - hits protected routes as an anonymous user."""
    return TestClient(app, raise_server_exceptions=True)


@pytest.fixture
def auth_client(app):
    """TestClient with a valid admin session pre-established."""
    c = TestClient(app, raise_server_exceptions=True)
    resp = c.post(
        "/login",
        data={"username": "testadmin", "password": "testpass", "next": "/"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, f"Login failed with status {resp.status_code}"
    return c
