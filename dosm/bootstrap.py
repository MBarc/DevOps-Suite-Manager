from __future__ import annotations

from pathlib import Path

import yaml

DEFAULT_CONFIG: dict = {
    "server": {"host": "127.0.0.1", "port": 8765},
    "llm": {
        "provider": "ollama",
        "base_url": "http://127.0.0.1:11434",
        "model": "qwen2.5:7b-instruct",
        "embedding_model": "bge-small-en-v1.5",
    },
    "secrets": {
        "backend": "local",
        "local_key_file": "config/secrets.key",
    },
    "enabled_modules": [],
}

SUBDIRS = [
    "config",
    "docs",
    "docs/drafts",
    "scripts",
    "modules",
    "resources",
    "data",
    "data/index",
    "data/action_log",
    "logs",
]

README_TEMPLATE = """\
# DOSM Home

This directory is the root that the DevOps Operations Suite Manager reads from.

- `docs/`       Markdown, text, and PDF documentation. Indexed into the LLM.
- `scripts/`    Executable scripts the agent can propose to run (with approval).
- `modules/`    Installed integration modules (Service Fabric, Dynatrace, etc.).
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
