"""Access-control rework data migration: the tenant ``admin`` role is renamed
to ``tenant_admin``, group mappings collapse to the baseline ``viewer``, and the
retired tenant-less ``platform_admin``-via-group rows are dropped. All idempotent
(``run_migrations`` is re-run on every startup)."""
from __future__ import annotations

from sqlalchemy import create_engine, text

from dosm.migrations import run_migrations
from dosm.models import Base


def _fresh_engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'app.db'}")
    Base.metadata.create_all(engine)
    run_migrations(engine)  # establish the migrated baseline (default tenant, etc.)
    return engine


def _default_tid(engine) -> int:
    with engine.begin() as conn:
        return int(conn.execute(
            text("SELECT id FROM tenants WHERE slug='default'")
        ).scalar_one())


def test_role_rename_and_group_collapse(tmp_path):
    engine = _fresh_engine(tmp_path)
    tid = _default_tid(engine)

    # Seed legacy rows as they'd exist on a pre-rework database.
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO users (username, password_hash, role, tenant_id, is_active,"
            " role_locked, auth_provider, prefs_json, created_at)"
            " VALUES ('legacy_admin', 'x', 'admin', :t, 1, 0, 'local', '{}', :ts),"
            "        ('legacy_op', 'x', 'operator', :t, 1, 0, 'local', '{}', :ts)"
        ), {"t": tid, "ts": "2026-01-01T00:00:00"})
        conn.execute(text(
            "INSERT INTO group_mappings (group_name, tenant_id, role, created_at)"
            " VALUES ('Team-Ops', :t, 'operator', :ts),"
            "        ('Team-Admins', :t, 'admin', :ts),"
            "        ('Platform-Supers', NULL, 'platform_admin', :ts)"
        ), {"t": tid, "ts": "2026-01-01T00:00:00"})

    applied = run_migrations(engine)

    # The applied list reports each idempotent step it actually performed.
    assert any(a.startswith("users.admin_to_tenant_admin_") for a in applied)
    assert any(a.startswith("group_mappings.collapse_to_viewer_") for a in applied)
    assert any(a.startswith("group_mappings.drop_platform_admin_") for a in applied)

    with engine.begin() as conn:
        roles = dict(conn.execute(text("SELECT username, role FROM users")).all())
        # 'admin' renamed; other roles untouched.
        assert roles["legacy_admin"] == "tenant_admin"
        assert roles["legacy_op"] == "operator"

        grants = dict(conn.execute(
            text("SELECT group_name, role FROM group_mappings")
        ).all())
        # Every surviving group grants only viewer...
        assert grants == {"Team-Ops": "viewer", "Team-Admins": "viewer"}
        # ...and the tenant-less platform_admin grant is gone.
        assert "Platform-Supers" not in grants


def test_migration_is_idempotent(tmp_path):
    engine = _fresh_engine(tmp_path)
    # A second run on an already-current DB renames/collapses/deletes nothing.
    applied = run_migrations(engine)
    assert not any(a.startswith("users.admin_to_tenant_admin_") for a in applied)
    assert not any(a.startswith("group_mappings.collapse_to_viewer_") for a in applied)
    assert not any(a.startswith("group_mappings.drop_platform_admin_") for a in applied)
