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
    model: str = "qwen2.5:7b-instruct"
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


class GuacamoleConfig(BaseModel):
    """Apache Guacamole HTML5 SSH/RDP/VNC integration.

    DOSM signs short-lived JSON connection blobs with a 128-bit shared key
    (auth-json extension). The Guacamole webapp accepts the blob, returns a
    session token, and DOSM iframes the resulting client URL.
    """

    enabled: bool = False
    base_url: str = "http://127.0.0.1:8080/guacamole"
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


class DocsIndexConfig(BaseModel):
    """Local docs ingestion: scan $DOSM_HOME/docs, chunk, embed, store."""

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
    terminals: TerminalsConfig = TerminalsConfig()
    docs_index: DocsIndexConfig = DocsIndexConfig()
    guacamole: GuacamoleConfig = GuacamoleConfig()
    ssh_command_policy: SSHPolicyConfig = SSHPolicyConfig()
    enabled_modules: list[str] = Field(default_factory=list)

    @property
    def docs_dir(self) -> Path:
        return self.home / "docs"

    @property
    def scripts_dir(self) -> Path:
        return self.home / "scripts"

    @property
    def modules_dir(self) -> Path:
        return self.home / "modules"

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
