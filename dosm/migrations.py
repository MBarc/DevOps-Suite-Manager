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
    # Explicit jumpbox role flag. Backfill: any host already referenced as a
    # jump_host_id by another host gets flagged so existing inventories keep
    # working without manual intervention.
    if _add_column_if_missing(
        engine,
        "hosts",
        "is_jumpbox",
        "is_jumpbox BOOLEAN NOT NULL DEFAULT 0",
    ):
        with engine.begin() as conn:
            conn.execute(text(
                "UPDATE hosts SET is_jumpbox = 1 WHERE id IN ("
                "SELECT DISTINCT jump_host_id FROM hosts WHERE jump_host_id IS NOT NULL)"
            ))
        applied.append("hosts.is_jumpbox")
    # Credential kind consolidation: collapse protocol-specific kinds into
    # protocol-agnostic ones (ssh_password/rdp_password/vnc_password → login,
    # api_token → pat). The UPDATEs are no-ops if already migrated.
    with engine.begin() as conn:
        r1 = conn.execute(text(
            "UPDATE credentials SET kind = 'login'"
            " WHERE kind IN ('ssh_password', 'rdp_password', 'vnc_password')"
        ))
        r2 = conn.execute(text(
            "UPDATE credentials SET kind = 'pat' WHERE kind = 'api_token'"
        ))
    if r1.rowcount + r2.rowcount > 0:
        applied.append("credentials.kind_rename")
    # RD Gateway support — Windows domain for RDP credentials
    if _add_column_if_missing(
        engine,
        "credentials",
        "domain",
        "domain VARCHAR(128)",
    ):
        applied.append("credentials.domain")
    # Phase 15 — Documentation vault: application taxonomy on documents
    # Note: SQLite ALTER TABLE ADD COLUMN does not enforce FK constraints;
    # the ORM relationship provides the association at the Python level.
    if _add_column_if_missing(
        engine,
        "documents",
        "application_id",
        "application_id INTEGER REFERENCES applications(id) ON DELETE SET NULL",
    ):
        applied.append("documents.application_id")
    if _add_column_if_missing(
        engine,
        "documents",
        "frontmatter_title",
        "frontmatter_title VARCHAR(255)",
    ):
        applied.append("documents.frontmatter_title")
    # Phase 14 redo — Organisation graph rebuilt on top of Active Directory.
    # The old shape had free-text head/email/parent. Detect it by the `head`
    # column and drop the table; Base.metadata.create_all rebuilds with the
    # new schema (ad_group_dn, manager_dn, sync state, member relationship).
    # Project decision: no production data exists yet, so a clean replace is
    # safe and avoids needing Alembic for the column drops + add.
    if _has_column(engine, "departments", "head"):
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS department_members"))
            conn.execute(text("DROP TABLE departments"))
        applied.append("departments.v2_clean_replace")
    # Phase 14 polish — per-member manager fields so the directory list
    # shows "who reports to whom" instead of just titles.
    if _add_column_if_missing(
        engine,
        "department_members",
        "manager_dn",
        "manager_dn VARCHAR(512)",
    ):
        applied.append("department_members.manager_dn")
    if _add_column_if_missing(
        engine,
        "department_members",
        "manager_name",
        "manager_name VARCHAR(255)",
    ):
        applied.append("department_members.manager_name")
    # Phase 16d — thinking trace: per-message JSON record of read-only
    # query tool calls the agent made before answering. Drives the
    # collapsible "Thinking…" bubble in the chat UI.
    if _add_column_if_missing(
        engine,
        "chat_messages",
        "thinking",
        "thinking TEXT",
    ):
        applied.append("chat_messages.thinking")
    # Total wall-clock generation time in milliseconds (LLM inference + tool
    # execution + DB write). Shown in the thinking bubble so the operator
    # can see actual wait time even when no query tools were called.
    if _add_column_if_missing(
        engine,
        "chat_messages",
        "generation_ms",
        "generation_ms INTEGER",
    ):
        applied.append("chat_messages.generation_ms")
    return applied
