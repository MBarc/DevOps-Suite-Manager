from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Literal

from pydantic import BaseModel, Field

OSName = Literal["windows", "linux", "darwin"]
Source = Literal["builtin", "user"]


class ModuleSpec(BaseModel):
    """Contents of a module's ``module.yaml``.

    A module package also exports a ``register(app, cfg)`` callable (defined
    in its ``__init__.py``) that the loader invokes after import.
    """

    name: str = Field(..., description="Unique identifier — used in config.enabled_modules.")
    version: str = "0.1.0"
    description: str = ""
    python_package: str | None = Field(
        None,
        description="Import name, if different from `name`. Defaults to `name`.",
    )
    python_deps: list[str] = Field(
        default_factory=list,
        description="Informational. User is responsible for installing these.",
    )
    os_constraints: list[OSName] = Field(
        default_factory=list,
        description="If non-empty, module is only loaded on listed OSes.",
    )
    capabilities: list[str] = Field(
        default_factory=list,
        description="Freeform tags: inventory, actions, health, docs, ...",
    )
    settings: dict = Field(
        default_factory=dict,
        description="Module-specific defaults; override via config.yaml modules.<name>.",
    )


@dataclass
class DiscoveredModule:
    spec: ModuleSpec
    source: Source
    root: Path  # directory containing module.yaml


@dataclass
class LoadedModule:
    spec: ModuleSpec
    source: Source
    root: Path
    package: ModuleType
    # Freeform dict the module populates via `register()` — e.g. actions,
    # inventory providers. Phase 7 defines concrete shapes.
    exports: dict = field(default_factory=dict)
