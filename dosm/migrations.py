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
    # DOSM-stack — services DOSM itself integrates with
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
    # RD Gateway support — Windows domain for RDP credentials
    if _add_column_if_missing(
        engine,
        "credentials",
        "domain",
        "domain VARCHAR(128)",
    ):
        applied.append("credentials.domain")
    # Phase 18 — File transfer as a host capability (sftp/ftp/ftps), separate
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
    # Network tools: ensure every built-in port definition exists.
    # INSERT OR IGNORE is idempotent — safe to run on both fresh installs and
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
        # Phase 18 — flag the file-transfer ports (FTP/explicit-FTPS on 21,
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
    # Seed the DOSM Server host (local execution — no SSH/WinRM needed).
    if "hosts" in insp.get_table_names():
        with engine.begin() as conn:
            exists = conn.execute(
                text("SELECT COUNT(*) FROM hosts WHERE protocol = 'local'")
            ).scalar()
            if not exists:
                now_s = datetime.now(UTC).isoformat()
                conn.execute(
                    text(
                        "INSERT OR IGNORE INTO hosts"
                        " (name, hostname, port, protocol, is_jumpbox, created_at, updated_at)"
                        " VALUES ('DOSM Server', '127.0.0.1', 0, 'local', 0, :ts, :ts)"
                    ),
                    {"ts": now_s},
                )
                applied.append("hosts.dosm_server_local")
    return applied
