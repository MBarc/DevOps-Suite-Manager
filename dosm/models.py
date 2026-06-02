from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


# ---- Users / auth ---------------------------------------------------------


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="operator")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<User {self.username} role={self.role}>"


# ---- Hosts inventory ------------------------------------------------------


host_tags = None  # placeholder to satisfy any tooling; real join defined below


class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)


class HostTag(Base):
    __tablename__ = "host_tags"

    host_id: Mapped[int] = mapped_column(ForeignKey("hosts.id", ondelete="CASCADE"), primary_key=True)
    tag_id: Mapped[int] = mapped_column(ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True)


class Credential(Base):
    __tablename__ = "credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # login | ssh_key | pat
    username: Mapped[str | None] = mapped_column(String(128), nullable=True)
    domain: Mapped[str | None] = mapped_column(String(128), nullable=True)
    secret_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow, nullable=False
    )


class CertSource(Base):
    """A cloud certificate source — Azure Key Vault / AWS ACM / GCP Certificate
    Manager (or a mock). Certificates are fetched live + cached, not persisted;
    this row holds the source's config. Auth is either a credential profile
    (``auth_mode='profile'`` + ``credential``) or the cloud SDK's ambient
    identity (``auth_mode='ambient'`` — managed identity / instance role)."""

    __tablename__ = "cert_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    # Non-secret provider config as JSON: vault_url / region / project+location.
    config_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    auth_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="profile")
    credential_id: Mapped[int | None] = mapped_column(
        ForeignKey("credentials.id", ondelete="SET NULL"), nullable=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow, nullable=False
    )

    credential: Mapped[Credential | None] = relationship(
        "Credential", foreign_keys=lambda: [CertSource.credential_id]
    )


class Host(Base):
    __tablename__ = "hosts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    hostname: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False, default=22)
    protocol: Mapped[str] = mapped_column(String(16), nullable=False, default="ssh")  # ssh | rdp | vnc
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    credential_id: Mapped[int | None] = mapped_column(
        ForeignKey("credentials.id", ondelete="SET NULL"), nullable=True
    )
    jump_host_id: Mapped[int | None] = mapped_column(
        ForeignKey("hosts.id", ondelete="SET NULL"), nullable=True
    )
    is_jumpbox: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    source_module: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # File transfer is a capability of the host, separate from its primary
    # remote-access protocol: an SSH box can also expose SFTP/FTP/FTPS. None =
    # not configured. ft_credential overrides the host credential when the FTP
    # login differs from the SSH login; falls back to ``credential``.
    ft_method: Mapped[str | None] = mapped_column(String(8), nullable=True)  # sftp | ftp | ftps
    ft_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ft_credential_id: Mapped[int | None] = mapped_column(
        ForeignKey("credentials.id", ondelete="SET NULL"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow, nullable=False
    )

    credential: Mapped[Credential | None] = relationship(
        "Credential", foreign_keys=lambda: [Host.credential_id]
    )
    ft_credential: Mapped[Credential | None] = relationship(
        "Credential", foreign_keys=lambda: [Host.ft_credential_id]
    )
    jump_host: Mapped[Host | None] = relationship(
        "Host", remote_side=lambda: [Host.id], foreign_keys=lambda: [Host.jump_host_id]
    )
    tags: Mapped[list[Tag]] = relationship(
        "Tag",
        secondary="host_tags",
        lazy="selectin",
        order_by="Tag.name",
    )


# ---- Organization graph ---------------------------------------------------


class Department(Base):
    """A team or business unit, sourced from an Active Directory group.

    Members and parent hierarchy are sync-populated, never user-edited.
    The user supplies the AD group name and the manager (an AD user); the
    sync engine resolves DNs, fetches member attributes, and walks the
    manager-of-manager chain to derive ``parent_id``.
    """

    __tablename__ = "departments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # AD group — what binds this dept to a real-world group of people.
    ad_group_name: Mapped[str] = mapped_column(String(255), nullable=False)
    ad_group_dn: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # Manager — set by user (input string), DN + cached attrs filled by sync.
    manager_input: Mapped[str] = mapped_column(String(255), nullable=False)
    manager_dn: Mapped[str | None] = mapped_column(String(512), nullable=True)
    manager_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    manager_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    manager_title: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Hierarchy: derived from manager chain at sync time. Never user-set.
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("departments.id", ondelete="SET NULL"), nullable=True
    )

    # Sync state
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    sync_status: Mapped[str] = mapped_column(String(16), nullable=False, default="never")
    # never | ok | error | pending

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow, nullable=False
    )

    parent: Mapped[Department | None] = relationship(
        "Department",
        back_populates="children",
        remote_side=lambda: [Department.id],
        foreign_keys=lambda: [Department.parent_id],
    )
    children: Mapped[list[Department]] = relationship(
        "Department",
        back_populates="parent",
        foreign_keys=lambda: [Department.parent_id],
        lazy="selectin",
    )
    members: Mapped[list[DepartmentMember]] = relationship(
        "DepartmentMember",
        back_populates="department",
        cascade="all, delete-orphan",
        order_by="DepartmentMember.display_name",
    )


class DepartmentMember(Base):
    """A person who belongs to a department's AD group.

    Synced from AD on demand. ``enabled=False`` indicates the AD account is
    disabled — the UI renders these with a strikethrough and tooltip rather
    than hiding them, so an operator looking at "who do I talk to" can still
    see the historical association.
    """

    __tablename__ = "department_members"
    __table_args__ = (
        UniqueConstraint("department_id", "user_dn", name="uq_dept_member_dn"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    department_id: Mapped[int] = mapped_column(
        ForeignKey("departments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_dn: Mapped[str] = mapped_column(String(512), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # AD `manager` attribute, captured at sync time so the directory list can
    # show each person's manager without a live LDAP round trip.
    manager_dn: Mapped[str | None] = mapped_column(String(512), nullable=True)
    manager_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)

    department: Mapped[Department] = relationship("Department", back_populates="members")


# ---- Docs index -----------------------------------------------------------


class Folder(Base):
    """Taxonomy label that groups related documentation (e.g. 'Service Fabric', 'Dynatrace')."""

    __tablename__ = "applications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    rel_path: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    modified_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")  # pending | indexed | error
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    folder_id: Mapped[int | None] = mapped_column(
        "application_id", ForeignKey("applications.id", ondelete="SET NULL"), nullable=True, index=True
    )
    frontmatter_title: Mapped[str | None] = mapped_column(String(255), nullable=True)

    folder: Mapped[Folder | None] = relationship("Folder")


class DocChunk(Base):
    __tablename__ = "doc_chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    doc_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ord: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    start_char: Mapped[int] = mapped_column(Integer, nullable=False)
    end_char: Mapped[int] = mapped_column(Integer, nullable=False)
    # float32 bytes, length = embedding_dim * 4. Null if embedder=none or errored.
    embedding: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)


# ---- LLM chat -------------------------------------------------------------


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="New chat")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow, nullable=False
    )
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="llm")  # llm | agent


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # user | assistant | system
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # JSON-encoded list[{rel_path, chunk_id, ord, score, snippet}]. Null for user msgs.
    citations: Mapped[str | None] = mapped_column(Text, nullable=True)
    # JSON-encoded list[{tool, args, ok, summary, data_preview, elapsed_ms}].
    # Records read-only query tool calls the agent made before answering.
    thinking: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Wall-clock milliseconds from start of _generate_reply to DB commit.
    generation_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False, index=True)
    ord: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class PlanCard(Base):
    """A proposed agent-mode action awaiting human review.

    Each agent assistant message can produce one or more plan cards. A card
    moves through pending -> approved/rejected -> executed/failed. The
    `effective_args` column holds the JSON args actually executed (which
    differ from `args` if the user used Edit before approving).
    """

    __tablename__ = "plan_cards"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    message_id: Mapped[int | None] = mapped_column(
        ForeignKey("chat_messages.id", ondelete="SET NULL"), nullable=True
    )
    tool: Mapped[str] = mapped_column(String(64), nullable=False)
    args: Mapped[str] = mapped_column(Text, nullable=False)              # JSON proposed by LLM
    effective_args: Mapped[str | None] = mapped_column(Text, nullable=True)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    rollback: Mapped[str | None] = mapped_column(Text, nullable=True)
    tier: Mapped[str] = mapped_column(String(16), nullable=False, default="safe")   # safe | elevated
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    # ^ pending | approved | rejected | executed | failed
    result: Mapped[str | None] = mapped_column(Text, nullable=True)       # JSON: stdout/stderr/exit/duration
    approver_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False, index=True)


# ---- Pipelines (CI/CD) ---------------------------------------------------


class Pipeline(Base):
    """A user-registered CI/CD pipeline that DOSM can trigger.

    `provider` discriminates the adapter (currently only github_actions).
    `config` is provider-specific JSON: for GitHub Actions
    {"owner", "repo", "workflow", "ref"}.
    """

    __tablename__ = "pipelines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="github_actions")
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    config: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    inputs_schema: Mapped[str | None] = mapped_column(Text, nullable=True)
    credential_id: Mapped[int | None] = mapped_column(
        ForeignKey("credentials.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow, nullable=False
    )

    credential: Mapped[Credential | None] = relationship("Credential")


class PipelineRun(Base):
    __tablename__ = "pipeline_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pipeline_id: Mapped[int] = mapped_column(
        ForeignKey("pipelines.id", ondelete="CASCADE"), nullable=False, index=True
    )
    external_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    # queued | running | success | failed | cancelled | skipped | unknown
    inputs: Mapped[str | None] = mapped_column(Text, nullable=True)
    html_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    triggered_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    triggered_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


# ---- Monitoring sources ---------------------------------------------------


class MonitoringSource(Base):
    """A configured monitoring tool tenant (Dynatrace env, Datadog org, or
    ServiceNow instance). Secrets are stored in the secrets backend; the
    columns here hold only the path references."""

    __tablename__ = "monitoring_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    tool: Mapped[str] = mapped_column(String(32), nullable=False)
    # dynatrace: base URL  |  datadog: site (e.g. datadoghq.com)  |  servicenow: base URL
    url: Mapped[str] = mapped_column(String(512), nullable=False)
    username: Mapped[str | None] = mapped_column(String(128), nullable=True)  # ServiceNow only
    # Paths in the secrets backend (not the values themselves)
    token_secret: Mapped[str | None] = mapped_column(String(255), nullable=True)
    token2_secret: Mapped[str | None] = mapped_column(String(255), nullable=True)  # Datadog app key
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow, nullable=False
    )


class MonitoringMatch(Base):
    """Persisted result of a host-check against a monitoring source — a local
    cache so repeat lookups don't re-hit the API. Served while ``checked_at`` is
    within the TTL; stale/missing entries trigger a fresh query (and a manual
    Refresh forces it). Identity/presence only — live alert state isn't cached."""

    __tablename__ = "monitoring_matches"
    __table_args__ = (UniqueConstraint("hostname", "source_id", name="uq_monmatch_host_src"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    hostname: Mapped[str] = mapped_column(String(255), nullable=False, index=True)  # lower-cased
    source_id: Mapped[int] = mapped_column(
        ForeignKey("monitoring_sources.id", ondelete="CASCADE"), nullable=False
    )
    found: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    entity_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    entity_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    entity_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    extra_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)


# ---- Secrets storage (local backend) --------------------------------------


class SecretBlob(Base):
    """Encrypted secret values for the Local secrets backend.

    The `value` column holds a Fernet token (URL-safe base64 bytes). Vault
    backend does not use this table.
    """

    __tablename__ = "secret_blobs"

    path: Mapped[str] = mapped_column(String(255), primary_key=True)
    value: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow, nullable=False
    )


# ---- Audit log ------------------------------------------------------------


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False, index=True)
    actor_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    target: Mapped[str | None] = mapped_column(String(255), nullable=True)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)


class RecordingSession(Base):
    """One user-initiated session journal (explicit start/stop)."""

    __tablename__ = "recording_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    started_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    slug: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    options_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    # Relative path from $DOSM_HOME to the final finalized journal file.
    # Null while the recording is still active (journal lives in tmp_dir).
    journal_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    # active | finalized | aborted


# ---- Network tools --------------------------------------------------------


class NetworkPort(Base):
    """Master port library — reusable definitions for network scans."""

    __tablename__ = "network_ports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    port_number: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    protocol: Mapped[str] = mapped_column(String(8), nullable=False, default="tcp")
    description: Mapped[str] = mapped_column(String(128), nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)


class NetworkScan(Base):
    """A saved network connectivity scan job."""

    __tablename__ = "network_scans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    # pending | running | completed | failed
    config_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    # config_json shape: {"sources":[host_id,…], "destinations":[{"type":
    #   "inventory"|"adhoc","host_id":int|null,"address":str,"label":str},…],
    #   "port_ids":[int,…]}
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    results: Mapped[list[NetworkScanResult]] = relationship(
        "NetworkScanResult", back_populates="scan", cascade="all, delete-orphan"
    )


class NetworkScanResult(Base):
    """One source to destination×port check within a NetworkScan."""

    __tablename__ = "network_scan_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scan_id: Mapped[int] = mapped_column(
        ForeignKey("network_scans.id", ondelete="CASCADE"), nullable=False, index=True
    )
    src_host_id: Mapped[int | None] = mapped_column(
        ForeignKey("hosts.id", ondelete="SET NULL"), nullable=True
    )
    src_label: Mapped[str] = mapped_column(String(128), nullable=False)
    dst_label: Mapped[str] = mapped_column(String(128), nullable=False)
    dst_address: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    protocol: Mapped[str] = mapped_column(String(8), nullable=False, default="tcp")
    reachable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_msg: Mapped[str | None] = mapped_column(Text, nullable=True)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    scan: Mapped[NetworkScan] = relationship("NetworkScan", back_populates="results")


__all__ = [
    "Base",
    "User",
    "Department",
    "DepartmentMember",
    "Tag",
    "HostTag",
    "Credential",
    "Host",
    "MonitoringSource",
    "SecretBlob",
    "AuditLog",
    "Document",
    "DocChunk",
    "Conversation",
    "ChatMessage",
    "PlanCard",
    "Pipeline",
    "PipelineRun",
    "RecordingSession",
    "NetworkPort",
    "NetworkScan",
    "NetworkScanResult",
]


# Enforce unique (host_id, tag_id) is the PK already, but keep explicit:
UniqueConstraint("host_id", "tag_id", name="uq_host_tags")
