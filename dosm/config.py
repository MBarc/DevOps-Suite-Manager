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
    vault_mount: str | None = None


class Config(BaseModel):
    home: Path = Field(..., description="Root $DOSM_HOME directory.")
    server: ServerConfig = ServerConfig()
    llm: LLMConfig = LLMConfig()
    secrets: SecretsConfig = SecretsConfig()
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
