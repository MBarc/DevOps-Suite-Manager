"""Idempotent schema migrations for SQLite.

DOSM uses ``Base.metadata.create_all`` to materialize new tables but that
won't add new columns to existing ones. Each phase that adds a column also
appends a migration here. Migrations are run once at startup and are no-ops
on a schema that already has the column.

This is deliberately lightweight — Alembic is the right answer once schema
churn warrants it; for now we want zero-config upgrades from a previous
DOSM_HOME.
"""
from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine


def _has_column(engine: Engine, table: str, column: str) -> bool:
    insp = inspect(engine)
    if table not in insp.get_table_names():
        return False
    return any(c["name"] == column for c in insp.get_columns(table))


def _add_column_if_missing(engine: Engine, table: str, column: str, ddl: str) -> bool:
    if _has_column(engine, table, column):
        return False
    with engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))
    return True


def run_migrations(engine: Engine) -> list[str]:
    """Apply all known column additions. Returns the list of migrations
    actually applied (empty if everything was already current)."""
    applied: list[str] = []
    # Phase 9 — jump host chains
    if _add_column_if_missing(
        engine,
        "hosts",
        "jump_host_id",
        "jump_host_id INTEGER REFERENCES hosts(id) ON DELETE SET NULL",
    ):
        applied.append("hosts.jump_host_id")
    return applied
