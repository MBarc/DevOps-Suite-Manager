from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8765


class LLMConfig(BaseModel):
    provider: str = "ollama"
    base_url: str = "http://127.0.0.1:11434"
    model: str = "qwen2.5:3b-instruct"
    embedding_model: str = "bge-small-en-v1.5"


class SecretsConfig(BaseModel):
    backend: str = "local"  # "local" | "vault"
    local_key_file: str = "config/secrets.key"
    vault_addr: str | None = None
    vault_token_env: str = "VAULT_TOKEN"
    vault_mount: str = "secret"
    vault_prefix: str = "dosm"


class AuthConfig(BaseModel):
    session_secret_file: str = "config/session.key"
    session_cookie: str = "dosm_session"
    session_max_age_seconds: int = 60 * 60 * 12  # 12h


class OktaConfig(BaseModel):
    """Okta OIDC single sign-on.

    Authentication only - group membership for authorization rides in on the
    ID token's ``groups`` claim (Okta federates AD), mapped to a DOSM role by
    ``RbacConfig.group_role_map``. The client secret is NOT stored here; it
    lives in the secrets backend under ``okta/client_secret``.
    """

    enabled: bool = False
    # e.g. https://your-org.okta.com/oauth2/default  (the authorization server
    # issuer; DOSM appends /.well-known/openid-configuration for discovery).
    issuer: str = ""
    client_id: str = ""
    # Path DOSM serves the OIDC redirect on; must be registered in the Okta app
    # as a sign-in redirect URI (scheme+host comes from the incoming request).
    redirect_path: str = "/auth/okta/callback"
    scopes: list[str] = Field(default_factory=lambda: ["openid", "profile", "email", "groups"])
    groups_claim: str = "groups"


class RbacConfig(BaseModel):
    """AD/Okta group → tenant membership (legacy single-tenant config form).

    The live group→tenant grants now live in the ``group_mappings`` DB table
    (managed from Access control), not here. Membership in a mapped group grants
    only the baseline ``viewer`` role within that group's tenant; elevation is a
    per-user action in Members. ``group_role_map`` is retained only for the
    one-time config→DB seed and the offline ``map_groups_to_role`` helper.
    """

    group_role_map: dict[str, str] = Field(default_factory=dict)
    # Role for an authenticated user who is in NONE of the mapped groups.
    # ``"none"`` (the secure default) denies access entirely - only members of a
    # mapped group can sign in. Set to ``"viewer"`` to instead grant everyone who
    # authenticates that baseline (Default tenant).
    default_role: str = "none"


class GuacamoleConfig(BaseModel):
    """Apache Guacamole HTML5 SSH/RDP/VNC integration.

    DOSM signs short-lived JSON connection blobs with a 128-bit shared key
    (auth-json extension). The Guacamole webapp accepts the blob, returns a
    session token, and DOSM iframes the resulting client URL.
    """

    enabled: bool = False
    base_url: str = "http://127.0.0.1:8080/guacamole"
    # public_url: browser-facing URL used for the iframe src. Defaults to
    # base_url. Set this when DOSM runs in Docker and base_url uses a service
    # name (e.g. http://guacamole:8080/guacamole) that the user's browser
    # cannot resolve - point public_url at the host-reachable address instead.
    public_url: str | None = None
    secret_key_file: str = "config/guacamole.key"
    session_ttl_seconds: int = 1800
    recordings_dir: str = "data/guacamole_recordings"
    # Forwarded to Guacamole as recording-related connection parameters.
    record_sessions: bool = True
    # Hostname Guacamole's container uses to reach DOSM's local-port forwards
    # (jump-host tunnels). Defaults to docker's host gateway. Set to a
    # specific IP / DNS name in production.
    dosm_reachable_host: str = "host.docker.internal"
    # Address DOSM binds tunnel listeners to. 0.0.0.0 lets the Guacamole
    # container reach them; lock this down to a private interface in prod.
    tunnel_bind_host: str = "0.0.0.0"


class MetricsConfig(BaseModel):
    """Defaults for the resource panel data sources."""

    poll_interval_seconds: float = 2.0
    # WinRM (Phase 8c): used for the resource panel's Windows-host source.
    winrm_port: int = 5985
    winrm_transport: str = "ntlm"  # basic | ntlm | kerberos
    winrm_use_https: bool = False
    winrm_timeout_seconds: float = 8.0


class SmbDocsConfig(BaseModel):
    """Connection settings for an SMB network-drive docs source.

    DOSM talks SMB2/3 directly (no OS mount), so this works from inside the
    Linux container. Auth reuses a ``login`` credential profile - the password
    lives in the secrets backend (referenced by the credential), never here.
    """

    server: str = ""  # file-server host or IP
    share: str = ""  # share name
    base_path: str = ""  # subpath within the share that is the docs root
    port: int = 445
    encrypt: bool = True
    credential_id: int | None = None  # -> Credential(kind="login")
    poll_interval_seconds: float = 60.0  # watcher polling cadence (no FS events over SMB)


class DocsIndexConfig(BaseModel):
    """Docs ingestion: scan the docs source, chunk, embed, store.

    ``source`` selects where docs files live: ``local`` ($DOSM_HOME/docs) or
    ``smb`` (a network share, see ``smb``). Defaults to local so existing
    installs are unaffected.
    """

    chunk_size_chars: int = 1800
    chunk_overlap_chars: int = 200
    include_globs: list[str] = Field(
        default_factory=lambda: ["**/*.md", "**/*.markdown", "**/*.txt", "**/*.pdf"]
    )
    exclude_globs: list[str] = Field(default_factory=lambda: ["drafts/**"])
    embedder: str = "fastembed"  # "fastembed" | "none"
    embedder_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dim: int = 384
    auto_index_on_startup: bool = True
    source: str = "local"  # "local" | "smb"
    smb: SmbDocsConfig = Field(default_factory=SmbDocsConfig)


class CustomTerminal(BaseModel):
    name: str
    command: list[str]
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None
    description: str | None = None


class TerminalsConfig(BaseModel):
    """Local shells launchable from the Terminals page (admin-only)."""

    enabled: bool = True
    auto_detect: bool = True
    record_by_default: bool = True
    recordings_dir: str = "data/terminal_recordings"
    custom: list[CustomTerminal] = Field(default_factory=list)


class CertsConfig(BaseModel):
    """Certificate inventory scanner settings."""

    expires_warn_days: int = 30
    expires_critical_days: int = 7
    scan_paths: list[str] = Field(default_factory=list)
    windows_stores: list[str] = Field(default_factory=lambda: ["MY", "ROOT", "CA"])


class RecordingConfig(BaseModel):
    """Session journal recording settings."""

    enabled: bool = True
    # Relative to $DOSM_HOME. Finalized journals land here so the docs indexer
    # picks them up automatically on next reindex.
    sessions_dir: str = "docs/sessions"
    # Temp directory for in-progress journals (not indexed until finalized).
    tmp_dir: str = "data/recording_tmp"


class PipelinesConfig(BaseModel):
    """Background run-status poller settings."""

    poller_enabled: bool = True
    poller_tick_seconds: float = 5.0
    poller_max_concurrent: int = 4
    poller_abandon_after_hours: int = 24


class ConfluenceConfig(BaseModel):
    """Background poller settings for Confluence space listeners.

    Listeners themselves live in the DB (per-tenant ``confluence_listeners``
    rows) and are read live each tick, so adding a listener needs no restart.
    Only these loop-level knobs are config (restart-gated).
    """

    poller_enabled: bool = True
    poller_tick_seconds: float = 300.0
    poller_max_concurrent: int = 3
    # A listener is only resynced if its last sync was at least this long ago.
    min_resync_seconds: float = 300.0


class DirectoryConfig(BaseModel):
    """Active Directory integration via a Windows jumpbox.

    DOSM doesn't speak LDAP/Kerberos directly. It opens a WinRM session to
    a designated Windows host (which must have RSAT-AD-PowerShell installed
    and be domain-joined), runs PowerShell ActiveDirectory cmdlets there,
    and parses the JSON output. The bind identity is whatever credential
    profile is attached to the chosen host.
    """

    # Host id of the AD jumpbox. None means "not configured" - the Org page
    # shows an empty state pointing the user at the configure flow.
    ad_jumpbox_host_id: int | None = None
    # Optional driver override. "winrm_jumpbox" is the production adapter.
    # "mock" returns canned fixtures and is used by tests; flipping it on
    # here also lets a developer build the UI without a real jumpbox.
    adapter: str = "winrm_jumpbox"  # winrm_jumpbox | mock
    powershell_timeout_seconds: float = 30.0


class SSHPolicyConfig(BaseModel):
    """Governs what `ssh_exec` actions can run without elevated confirmation.

    Patterns are shell globs matched against the full command string. Off-list
    commands are still allowed in agent mode, but require a typed confirmation
    (the host name) in the plan card.
    """

    allow_list: list[str] = Field(
        default_factory=lambda: [
            "uptime",
            "whoami",
            "hostname",
            "id",
            "date",
            "df -h",
            "df -h *",
            "free -m",
            "free -h",
            "ps auxf",
            "ps -ef",
            "ls *",
            "cat /etc/os-release",
            "cat /proc/cpuinfo",
            "cat /proc/meminfo",
            "tail -n [0-9]* *",
            "journalctl --since * --no-pager",
            "journalctl -u * --no-pager",
            "systemctl status *",
            "systemctl is-active *",
        ]
    )
    require_confirmation_off_list: bool = True
    confirmation_field: str = "host_name"  # what the user types to confirm


class Config(BaseModel):
    home: Path = Field(..., description="Root $DOSM_HOME directory.")
    server: ServerConfig = ServerConfig()
    llm: LLMConfig = LLMConfig()
    secrets: SecretsConfig = SecretsConfig()
    auth: AuthConfig = AuthConfig()
    okta: OktaConfig = OktaConfig()
    rbac: RbacConfig = RbacConfig()
    terminals: TerminalsConfig = TerminalsConfig()
    recording: RecordingConfig = RecordingConfig()
    docs_index: DocsIndexConfig = DocsIndexConfig()
    guacamole: GuacamoleConfig = GuacamoleConfig()
    metrics: MetricsConfig = MetricsConfig()
    certs: CertsConfig = CertsConfig()
    pipelines: PipelinesConfig = PipelinesConfig()
    confluence: ConfluenceConfig = ConfluenceConfig()
    directory: DirectoryConfig = DirectoryConfig()
    ssh_command_policy: SSHPolicyConfig = SSHPolicyConfig()
    # cli_tools is a flat {tool_id: bool} map - Settings page toggles for
    # the CLI catalog. Enabled tools surface on the Terminals page.
    cli_tools: dict[str, bool] = Field(default_factory=dict)

    @property
    def docs_dir(self) -> Path:
        return self.home / "docs"

    @property
    def scripts_dir(self) -> Path:
        return self.home / "scripts"

    @property
    def resources_dir(self) -> Path:
        return self.home / "resources"

    @property
    def data_dir(self) -> Path:
        return self.home / "data"

    @property
    def logs_dir(self) -> Path:
        return self.home / "logs"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.db"


def resolve_home(explicit: str | Path | None = None) -> Path:
    if explicit is not None:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get("DOSM_HOME")
    if env:
        return Path(env).expanduser().resolve()
    raise RuntimeError(
        "DOSM_HOME is not set. Run `dosm init <path>` and export DOSM_HOME="
        "<path>, or pass --home."
    )


def load_config(home: str | Path | None = None) -> Config:
    home_path = resolve_home(home)
    cfg_file = home_path / "config.yaml"
    if not cfg_file.exists():
        raise FileNotFoundError(
            f"No config.yaml at {cfg_file}. Run `dosm init {home_path}` first."
        )
    raw = yaml.safe_load(cfg_file.read_text()) or {}
    raw["home"] = home_path
    return Config.model_validate(raw)


@lru_cache(maxsize=1)
def get_config() -> Config:
    return load_config()


def update_config_yaml(home: Path, updates: dict) -> None:
    """Shallow-merge `updates` into `$DOSM_HOME/config.yaml` and rewrite.

    Keys in `updates` replace the matching top-level keys in the existing
    file. Sub-keys not mentioned in `updates` are preserved by value, so a
    settings page can rewrite just `cli_tools` without touching anything
    else. The cached `get_config` value is invalidated so callers see fresh
    state on the next request.
    """
    cfg_file = home / "config.yaml"
    raw = yaml.safe_load(cfg_file.read_text()) if cfg_file.exists() else {}
    if not isinstance(raw, dict):
        raw = {}
    raw.update(updates)
    cfg_file.write_text(yaml.safe_dump(raw, sort_keys=False))
    get_config.cache_clear()
