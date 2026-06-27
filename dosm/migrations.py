"""Idempotent schema migrations for SQLite.


DOSM uses ``Base.metadata.create_all`` to materialize new tables but that
won't add new columns to existing ones. Each phase that adds a column also
appends a migration here. Migrations are run once at startup and are no-ops
on a schema that already has the column.

This is deliberately lightweight - Alembic is the right answer once schema
churn warrants it; for now we want zero-config upgrades from a previous
DOSM_HOME.
"""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

_NETWORK_PORT_DEFAULTS = [
    # ICMP
    (0, "icmp", "ICMP Ping", True),
    # Standard services
    (21, "tcp", "FTP / explicit FTPS", True),
    (22, "tcp", "SSH / SFTP", True),
    (25, "tcp", "SMTP", False),
    (53, "tcp", "DNS", False),
    (80, "tcp", "HTTP", True),
    (443, "tcp", "HTTPS", True),
    (445, "tcp", "SMB / Azure Files", False),
    # SQL & relational databases
    (1433, "tcp", "SQL Server / Azure SQL", False),
    (1521, "tcp", "Oracle TNS", False),
    (3306, "tcp", "MySQL", False),
    (5432, "tcp", "PostgreSQL", False),
    # NoSQL / caches
    (6379, "tcp", "Redis", False),
    (6380, "tcp", "Redis TLS / Azure Cache for Redis", False),
    (9200, "tcp", "Elasticsearch / OpenSearch", False),
    (9300, "tcp", "Elasticsearch Cluster", False),
    (27017, "tcp", "MongoDB", False),
    # Remote access & management
    (3389, "tcp", "RDP", True),
    (5900, "tcp", "VNC (display :0)", True),
    (5901, "tcp", "VNC (display :1)", False),
    (5985, "tcp", "WinRM HTTP", False),
    (5986, "tcp", "WinRM HTTPS", False),
    # Messaging / IoT / cloud networking
    (2049, "tcp", "NFS / AWS EFS", False),
    (5671, "tcp", "AMQP TLS / Azure Service Bus", False),
    (5672, "tcp", "AMQP / RabbitMQ", False),
    (8883, "tcp", "MQTT TLS / Azure IoT Hub", False),
    # App servers
    (7001, "tcp", "Oracle WebLogic HTTP", False),
    (7002, "tcp", "Oracle WebLogic HTTPS", False),
    (8080, "tcp", "HTTP Alt", False),
    (8443, "tcp", "HTTPS Alt", False),
    # Kubernetes / etcd
    (2379, "tcp", "etcd Client", False),
    (2380, "tcp", "etcd Peer", False),
    (6443, "tcp", "Kubernetes API", False),
    (10250, "tcp", "Kubelet API", False),
    # Observability (Prometheus ecosystem + Grafana)
    (3000, "tcp", "Grafana", False),
    (9090, "tcp", "Prometheus", False),
    (9091, "tcp", "Prometheus Pushgateway", False),
    (9093, "tcp", "Alertmanager", False),
    (9100, "tcp", "Node Exporter", False),
    # Dynatrace
    (9998, "tcp", "Dynatrace Cluster Node", False),
    (9999, "tcp", "Dynatrace ActiveGate", False),
    (14499, "tcp", "Dynatrace OneAgent to ActiveGate", False),
    # Git / GitHub Enterprise
    (9418, "tcp", "Git Protocol (git://)", False),
    # Terraform Enterprise (Replicated admin console)
    (8800, "tcp", "Terraform Enterprise Admin", False),
    # Octopus Deploy
    (10933, "tcp", "Octopus Tentacle (listening)", False),
    (10943, "tcp", "Octopus Tentacle (polling)", False),
    # DOSM-stack - services DOSM itself integrates with
    (4822, "tcp", "Guacamole guacd", False),
    (8200, "tcp", "HashiCorp Vault", False),
    (8201, "tcp", "HashiCorp Vault (cluster)", False),
    (11434, "tcp", "Ollama API", False),
    (19000, "tcp", "Service Fabric (client)", False),
    (19080, "tcp", "Service Fabric Explorer / HTTP gateway", False),
    # Active Directory / LDAP (org-directory feature)
    (88, "tcp", "Kerberos", False),
    (135, "tcp", "MSRPC / AD Endpoint Mapper", False),
    (389, "tcp", "LDAP", False),
    (636, "tcp", "LDAPS", False),
    (3268, "tcp", "AD Global Catalog", False),
    (3269, "tcp", "AD Global Catalog (TLS)", False),
    # Core infrastructure services
    (23, "tcp", "Telnet", False),
    (123, "udp", "NTP", False),
    (137, "udp", "NetBIOS Name", False),
    (138, "udp", "NetBIOS Datagram", False),
    (139, "tcp", "NetBIOS Session", False),
    (161, "udp", "SNMP", False),
    (162, "udp", "SNMP Trap", False),
    (465, "tcp", "SMTPS", False),
    (514, "udp", "Syslog", False),
    (587, "tcp", "SMTP Submission", False),
    (873, "tcp", "rsync", False),
    (993, "tcp", "IMAPS", False),
    (995, "tcp", "POP3S", False),
    (3260, "tcp", "iSCSI", False),
    # Data stores & messaging
    (2181, "tcp", "ZooKeeper", False),
    (2375, "tcp", "Docker daemon (plain)", False),
    (2376, "tcp", "Docker daemon (TLS)", False),
    (8500, "tcp", "Consul HTTP API", False),
    (9042, "tcp", "Cassandra CQL", False),
    (9092, "tcp", "Kafka", False),
    (11211, "tcp", "Memcached", False),
    (15672, "tcp", "RabbitMQ Management", False),
    (50000, "tcp", "IBM DB2", False),
]


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


# Operational tables that gain a (NOT NULL) ``tenant_id`` and/or a per-tenant
# UNIQUE constraint. SQLite cannot ALTER those in place, so on upgrade we drop
# and let ``Base.metadata.create_all`` rebuild them empty (data loss accepted -
# the same clean-replace strategy as ``departments.v2_clean_replace``). Listed
# child-first so foreign-key references are gone before their parents drop.
_MULTITENANT_DROP_ORDER = [
    # children
    "department_members",
    "doc_chunks",
    "chat_messages",
    "plan_cards",
    "pipeline_runs",
    "pipeline_payloads",
    "monitoring_matches",
    "network_scan_results",
    "host_tags",
    # parents
    "hosts",
    "credentials",
    "cert_sources",
    "org_units",
    "departments",
    "applications",
    "documents",
    "conversations",
    "pipelines",
    "monitoring_sources",
    "recording_sessions",
    "network_scans",
    "tags",
]


def _ensure_default_tenant(engine: Engine) -> int:
    """Return the id of the Default tenant, creating it if absent. Holds all
    pre-multi-tenancy data and is the fallback assignment for migrated users."""
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT id FROM tenants WHERE slug = 'default'")
        ).first()
        if row is not None:
            return int(row[0])
        now_s = datetime.now(UTC).isoformat()
        res = conn.execute(
            text(
                "INSERT INTO tenants (name, slug, description, is_active, created_at)"
                " VALUES ('Default', 'default',"
                " 'Default tenant (holds pre-multi-tenancy data)', 1, :ts)"
            ),
            {"ts": now_s},
        )
        return int(res.lastrowid)


def _migrate_multitenant(engine: Engine) -> list[str]:
    """Phase 24a - introduce multi-tenancy.

    Idempotent. On a fresh install ``create_all`` already builds every table
    with ``tenant_id`` + per-tenant UNIQUE constraints, so this only ensures the
    Default tenant exists and seeds the local DOSM Server host. On upgrade from a
    single-tenant DB it: (1) ensures the Default tenant, (2) adds ``tenant_id`` /
    ``role_locked`` to ``users`` and promotes existing admins to the tenant-less
    ``platform_admin`` role, (3) adds the nullable ``audit_log.tenant_id``, and
    (4) drops the operational tables whose schema changed so the caller's
    ``create_all`` rebuilds them tenant-scoped (data loss accepted)."""
    from dosm.models import Base

    insp = inspect(engine)
    tables = set(insp.get_table_names())
    if "tenants" not in tables:  # pragma: no cover - create_all runs first
        return []

    applied: list[str] = []
    default_tid = _ensure_default_tenant(engine)

    # users: add tenant scope + role lock, then backfill.
    users_changed = False
    if _add_column_if_missing(
        engine, "users", "tenant_id",
        "tenant_id INTEGER REFERENCES tenants(id) ON DELETE CASCADE",
    ):
        users_changed = True
    if _add_column_if_missing(
        engine, "users", "role_locked", "role_locked BOOLEAN NOT NULL DEFAULT 0"
    ):
        users_changed = True
    if users_changed:
        with engine.begin() as conn:
            # Pre-multi-tenant admins were effectively platform-wide -> promote
            # them to the tenant-less platform_admin role.
            conn.execute(text(
                "UPDATE users SET role = 'platform_admin', tenant_id = NULL"
                " WHERE role = 'admin'"
            ))
            # Everyone else lands in the Default tenant.
            conn.execute(
                text(
                    "UPDATE users SET tenant_id = :t"
                    " WHERE tenant_id IS NULL AND role != 'platform_admin'"
                ),
                {"t": default_tid},
            )
        applied.append("users.multitenant")

    # audit_log: nullable tenant scope (no rebuild - addable in place).
    if _add_column_if_missing(
        engine, "audit_log", "tenant_id",
        "tenant_id INTEGER REFERENCES tenants(id) ON DELETE SET NULL",
    ):
        applied.append("audit_log.tenant_id")

    # Rebuild operational tables that gained tenant_id / per-tenant UNIQUE.
    # Detect the old schema by a representative table missing tenant_id.
    if "hosts" in tables and not _has_column(engine, "hosts", "tenant_id"):
        with engine.begin() as conn:
            for tbl in _MULTITENANT_DROP_ORDER:
                conn.execute(text(f"DROP TABLE IF EXISTS {tbl}"))
        # Recreate the dropped tables (and any others) with the current schema.
        Base.metadata.create_all(engine)
        applied.append("multitenant.rebuild_operational_tables")

    return applied


def run_migrations(engine: Engine) -> list[str]:
    """Apply all known column additions. Returns the list of migrations
    actually applied (empty if everything was already current)."""
    applied: list[str] = []
    # Phase 24a - multi-tenancy. Runs first so the operational tables are
    # rebuilt tenant-scoped before the legacy column-adds + seeds below (which
    # then become no-ops on the freshly-created schema).
    applied.extend(_migrate_multitenant(engine))
    # Phase 24b+: group_mappings.tenant_id becomes nullable so a group can grant
    # the tenant-less platform_admin role. SQLite can't relax NOT NULL in place;
    # rebuild the table (mappings are re-addable / re-seeded from config).
    _insp = inspect(engine)
    if "group_mappings" in _insp.get_table_names():
        tid_col = next(
            (c for c in _insp.get_columns("group_mappings") if c["name"] == "tenant_id"),
            None,
        )
        if tid_col is not None and not tid_col["nullable"]:
            from dosm.models import Base
            with engine.begin() as conn:
                conn.execute(text("DROP TABLE group_mappings"))
            Base.metadata.create_all(engine)
            applied.append("group_mappings.tenant_id_nullable")
    # Phase 9 - jump host chains
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
    # protocol-agnostic ones (ssh_password/rdp_password/vnc_password to login,
    # api_token to pat). The UPDATEs are no-ops if already migrated.
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
    # RD Gateway support - Windows domain for RDP credentials
    if _add_column_if_missing(
        engine,
        "credentials",
        "domain",
        "domain VARCHAR(128)",
    ):
        applied.append("credentials.domain")
    # Phase 18 - File transfer as a host capability (sftp/ftp/ftps), separate
    # from the host's primary remote-access protocol. ft_credential_id overrides
    # the host credential when the FTP login differs from the SSH login.
    if _add_column_if_missing(engine, "hosts", "ft_method", "ft_method VARCHAR(8)"):
        applied.append("hosts.ft_method")
    if _add_column_if_missing(engine, "hosts", "ft_port", "ft_port INTEGER"):
        applied.append("hosts.ft_port")
    if _add_column_if_missing(
        engine,
        "hosts",
        "ft_credential_id",
        "ft_credential_id INTEGER REFERENCES credentials(id) ON DELETE SET NULL",
    ):
        applied.append("hosts.ft_credential_id")
    # Host organisation: 3-tier application -> environment -> unit tree. The
    # ``org_units`` table is created by create_all (new table); this adds the
    # back-reference column to the existing ``hosts`` table. SQLite does not
    # enforce the FK on an added column - the ORM relationship + repo provide
    # the association and ON DELETE SET NULL semantics at create_all time.
    if _add_column_if_missing(
        engine,
        "hosts",
        "org_unit_id",
        "org_unit_id INTEGER REFERENCES org_units(id) ON DELETE SET NULL",
    ):
        applied.append("hosts.org_unit_id")
    # Phase 15 - Documentation vault: application taxonomy on documents
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
    # Phase 14 redo - Organisation graph rebuilt on top of Active Directory.
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
    # Phase 14 polish - per-member manager fields so the directory list
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
    # Phase 16d - thinking trace: per-message JSON record of read-only
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
    # Network tools: ensure every built-in port definition exists.
    # INSERT OR IGNORE is idempotent - safe to run on both fresh installs and
    # existing databases that were seeded before new entries were added.
    insp = inspect(engine)
    if "network_ports" in insp.get_table_names():
        now = datetime.now(UTC).isoformat()
        inserted = 0
        with engine.begin() as conn:
            for pn, pr, desc, isd in _NETWORK_PORT_DEFAULTS:
                result = conn.execute(
                    text(
                        "INSERT OR IGNORE INTO network_ports"
                        " (port_number, protocol, description, is_default, created_at)"
                        " VALUES (:pn, :pr, :desc, :isd, :ts)"
                    ),
                    {"pn": pn, "pr": pr, "desc": desc, "isd": int(isd), "ts": now},
                )
                inserted += result.rowcount
        if inserted:
            applied.append(f"network_ports.seeded_{inserted}")
        # Phase 18 - flag the file-transfer ports (FTP/explicit-FTPS on 21,
        # SSH/SFTP on 22) as defaults with FT-aware labels. Gated on the old
        # description so it fires once and never overrides a user-edited label.
        with engine.begin() as conn:
            ft = 0
            ft += conn.execute(text(
                "UPDATE network_ports SET description = 'FTP / explicit FTPS', is_default = 1"
                " WHERE port_number = 21 AND description = 'FTP'"
            )).rowcount
            ft += conn.execute(text(
                "UPDATE network_ports SET description = 'SSH / SFTP'"
                " WHERE port_number = 22 AND description = 'SSH'"
            )).rowcount
        if ft:
            applied.append("network_ports.ft_defaults")
    # Seed the DOSM Server host (local execution - no SSH/WinRM needed), one per
    # tenant. Each tenant gets its own local-execution host so tenant-scoped
    # pipelines/terminals can target "their" DOSM Server.
    if "hosts" in insp.get_table_names() and "tenants" in insp.get_table_names():
        with engine.begin() as conn:
            now_s = datetime.now(UTC).isoformat()
            tenant_ids = [
                int(r[0]) for r in conn.execute(text("SELECT id FROM tenants")).all()
            ]
            seeded = 0
            for tid in tenant_ids:
                exists = conn.execute(
                    text(
                        "SELECT COUNT(*) FROM hosts"
                        " WHERE protocol = 'local' AND tenant_id = :t"
                    ),
                    {"t": tid},
                ).scalar()
                if not exists:
                    conn.execute(
                        text(
                            "INSERT INTO hosts"
                            " (name, tenant_id, hostname, port, protocol, is_jumpbox,"
                            "  created_at, updated_at)"
                            " VALUES ('DOSM Server', :t, '127.0.0.1', 0, 'local', 0,"
                            "  :ts, :ts)"
                        ),
                        {"t": tid, "ts": now_s},
                    )
                    seeded += 1
            if seeded:
                applied.append(f"hosts.dosm_server_local_{seeded}")
    # RBAC - per-credential ownership + visibility. Existing rows default to
    # ``shared`` with no owner, preserving the pre-RBAC behaviour where every
    # credential was visible to all. ``private`` credentials are visible only to
    # their owner (and admins).
    if _add_column_if_missing(
        engine,
        "credentials",
        "owner_id",
        "owner_id INTEGER REFERENCES users(id) ON DELETE SET NULL",
    ):
        applied.append("credentials.owner_id")
    if _add_column_if_missing(
        engine,
        "credentials",
        "visibility",
        "visibility VARCHAR(16) NOT NULL DEFAULT 'shared'",
    ):
        applied.append("credentials.visibility")
    # Pipelines RBAC - per-pipeline ownership + visibility (mirrors credentials).
    # Existing rows default to ``shared`` with no owner (visible to the tenant).
    if _add_column_if_missing(
        engine,
        "pipelines",
        "owner_id",
        "owner_id INTEGER REFERENCES users(id) ON DELETE SET NULL",
    ):
        applied.append("pipelines.owner_id")
    if _add_column_if_missing(
        engine,
        "pipelines",
        "visibility",
        "visibility VARCHAR(16) NOT NULL DEFAULT 'shared'",
    ):
        applied.append("pipelines.visibility")
    # RBAC - per-user UI preferences (private to each user).
    if _add_column_if_missing(
        engine,
        "users",
        "prefs_json",
        "prefs_json TEXT NOT NULL DEFAULT '{}'",
    ):
        applied.append("users.prefs_json")
    # Phase 21b - Okta SSO identity columns. Existing local users keep
    # auth_provider='local'; SSO users are JIT-provisioned with okta_sub set.
    if _add_column_if_missing(
        engine,
        "users",
        "auth_provider",
        "auth_provider VARCHAR(16) NOT NULL DEFAULT 'local'",
    ):
        applied.append("users.auth_provider")
    if _add_column_if_missing(engine, "users", "okta_sub", "okta_sub VARCHAR(255)"):
        # Enforce one DOSM account per Okta subject. NULLs (local users) are
        # distinct in SQLite, so any number of local accounts coexist.
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_okta_sub ON users(okta_sub)"
            ))
        applied.append("users.okta_sub")
    if _add_column_if_missing(engine, "users", "email", "email VARCHAR(255)"):
        applied.append("users.email")
    if _add_column_if_missing(engine, "users", "display_name", "display_name VARCHAR(255)"):
        applied.append("users.display_name")
    if _add_column_if_missing(engine, "users", "last_login", "last_login DATETIME"):
        applied.append("users.last_login")
    # Access-control rework: the tenant-scoped admin role is renamed ``admin`` ->
    # ``tenant_admin`` for clarity (it was always tenant-confined, distinct from
    # the cross-tenant ``platform_admin``). At the same time, AD/Okta group
    # mappings stop conferring a chosen role: every group now grants only the
    # baseline ``viewer`` within its tenant, and per-user elevation moved to the
    # Members page. So (1) rename existing admin users, (2) collapse every
    # tenant-scoped group mapping to ``viewer``, and (3) delete the retired
    # tenant-less ``platform_admin``-via-group rows (that grant path is gone -
    # platform_admin is now only assigned per-user in Members). All idempotent.
    tbls = set(inspect(engine).get_table_names())
    with engine.begin() as conn:
        renamed = 0
        if "users" in tbls:
            renamed += conn.execute(text(
                "UPDATE users SET role = 'tenant_admin' WHERE role = 'admin'"
            )).rowcount
        collapsed = deleted = 0
        if "group_mappings" in tbls:
            collapsed = conn.execute(text(
                "UPDATE group_mappings SET role = 'viewer'"
                " WHERE tenant_id IS NOT NULL AND role != 'viewer'"
            )).rowcount
            deleted = conn.execute(text(
                "DELETE FROM group_mappings WHERE tenant_id IS NULL"
            )).rowcount
    if renamed:
        applied.append(f"users.admin_to_tenant_admin_{renamed}")
    if collapsed:
        applied.append(f"group_mappings.collapse_to_viewer_{collapsed}")
    if deleted:
        applied.append(f"group_mappings.drop_platform_admin_{deleted}")
    return applied
