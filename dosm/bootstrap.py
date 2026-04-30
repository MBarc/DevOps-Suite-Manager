from __future__ import annotations

from pathlib import Path

import yaml

DEFAULT_CONFIG: dict = {
    "server": {"host": "127.0.0.1", "port": 8765},
    "llm": {
        "provider": "ollama",
        "base_url": "http://127.0.0.1:11434",
        "model": "qwen2.5:3b-instruct",
        "embedding_model": "bge-small-en-v1.5",
    },
    "secrets": {
        "backend": "local",
        "local_key_file": "config/secrets.key",
        "vault_addr": None,
        "vault_token_env": "VAULT_TOKEN",
        "vault_mount": "secret",
        "vault_prefix": "dosm",
    },
    "auth": {
        "session_secret_file": "config/session.key",
        "session_cookie": "dosm_session",
        "session_max_age_seconds": 43200,
    },
    "terminals": {
        "enabled": True,
        "auto_detect": True,
        "record_by_default": True,
        "recordings_dir": "data/terminal_recordings",
        "custom": [],
    },
    "docs_index": {
        "chunk_size_chars": 1800,
        "chunk_overlap_chars": 200,
        "include_globs": ["**/*.md", "**/*.markdown", "**/*.txt", "**/*.pdf"],
        "exclude_globs": ["drafts/**"],
        "embedder": "fastembed",
        "embedder_model": "BAAI/bge-small-en-v1.5",
        "embedding_dim": 384,
        "auto_index_on_startup": True,
    },
    "guacamole": {
        "enabled": False,
        "base_url": "http://127.0.0.1:8080/guacamole",
        "secret_key_file": "config/guacamole.key",
        "session_ttl_seconds": 1800,
        "recordings_dir": "data/guacamole_recordings",
        "record_sessions": True,
        "dosm_reachable_host": "host.docker.internal",
        "tunnel_bind_host": "0.0.0.0",
    },
    "metrics": {
        "poll_interval_seconds": 2.0,
        "winrm_port": 5985,
        "winrm_transport": "ntlm",
        "winrm_use_https": False,
        "winrm_timeout_seconds": 8.0,
    },
    "ssh_command_policy": {
        "require_confirmation_off_list": True,
        "confirmation_field": "host_name",
        "allow_list": [
            "uptime",
            "whoami",
            "hostname",
            "df -h",
            "df -h *",
            "free -h",
            "ps -ef",
            "ls *",
            "cat /etc/os-release",
            "tail -n [0-9]* *",
            "journalctl --since * --no-pager",
            "systemctl status *",
        ],
    },
}

SUBDIRS = [
    "config",
    "docs",
    "docs/drafts",
    "docs/_unfiled",   # default landing dir for vault docs with no application
    "scripts",
    "resources",
    "data",
    "data/index",
    "data/action_log",
    "data/terminal_recordings",
    "data/guacamole_recordings",
    "logs",
]

README_TEMPLATE = """\
# DOSM Home

This directory is the root that the DevOps Operations Suite Manager reads from.

- `docs/`       Markdown, text, and PDF documentation. Indexed into the LLM.
- `scripts/`    Executable scripts the agent can propose to run (with approval).
- `resources/`  Any other files you want alongside your ops workspace.
- `data/`       Application state: SQLite DB, vector index, action log.
- `logs/`       App logs.
- `config/`     Secrets key file and any per-host config.
- `config.yaml` Main app configuration.
"""


def initialize_home(home: Path, *, force: bool = False) -> list[Path]:
    """Create the $DOSM_HOME directory layout and default config.yaml.

    Returns the list of paths that were newly created.
    """
    home = home.expanduser().resolve()
    created: list[Path] = []

    if not home.exists():
        home.mkdir(parents=True)
        created.append(home)

    for sub in SUBDIRS:
        p = home / sub
        if not p.exists():
            p.mkdir(parents=True)
            created.append(p)

    cfg_path = home / "config.yaml"
    if force or not cfg_path.exists():
        cfg_path.write_text(yaml.safe_dump(DEFAULT_CONFIG, sort_keys=False))
        created.append(cfg_path)

    readme_path = home / "README.md"
    if force or not readme_path.exists():
        readme_path.write_text(README_TEMPLATE)
        created.append(readme_path)

    return created
