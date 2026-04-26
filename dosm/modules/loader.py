from __future__ import annotations

import importlib
import importlib.util
import platform
import sys
from pathlib import Path

import yaml
from fastapi import FastAPI

from dosm.config import Config
from dosm.modules.contract import DiscoveredModule, LoadedModule, ModuleSpec
from dosm.modules.registry import get_registry


def _current_os() -> str:
    system = platform.system().lower()
    if system == "darwin":
        return "darwin"
    if system == "windows":
        return "windows"
    return "linux"


def _parse_spec(module_dir: Path) -> ModuleSpec | None:
    yaml_path = module_dir / "module.yaml"
    if not yaml_path.exists():
        return None
    raw = yaml.safe_load(yaml_path.read_text()) or {}
    return ModuleSpec.model_validate(raw)


def _scan_dir(root: Path, source: str) -> list[DiscoveredModule]:
    found: list[DiscoveredModule] = []
    if not root.exists():
        return found
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.name.startswith((".", "_")):
            continue
        spec = _parse_spec(entry)
        if spec is None:
            continue
        found.append(DiscoveredModule(spec=spec, source=source, root=entry))
    return found


def builtin_modules_dir() -> Path:
    """Directory holding first-party bundled modules (inside the dosm package)."""
    return Path(__file__).resolve().parent / "builtin"


def discover_modules(cfg: Config) -> list[DiscoveredModule]:
    """Discover all modules from bundled + $DOSM_HOME/modules, returning both.

    If a name collides, the user-installed module wins (so you can override
    a builtin by dropping one into $DOSM_HOME/modules/).
    """
    found: dict[str, DiscoveredModule] = {}
    for d in _scan_dir(builtin_modules_dir(), source="builtin"):
        found[d.spec.name] = d
    for d in _scan_dir(cfg.modules_dir, source="user"):
        found[d.spec.name] = d  # user overrides builtin
    results = sorted(found.values(), key=lambda d: d.spec.name)
    get_registry().set_discovered(results)
    return results


def _import_module_package(d: DiscoveredModule) -> object:
    """Import a module package, handling both builtin (on sys.path already)
    and user modules (parent dir added ad hoc)."""
    import_name = d.spec.python_package or d.spec.name
    if d.source == "builtin":
        # Builtin modules live under dosm.modules.builtin.<name>
        qualified = f"dosm.modules.builtin.{import_name}"
        return importlib.import_module(qualified)
    # User modules: ensure parent dir on sys.path once.
    parent = str(d.root.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    return importlib.import_module(import_name)


def _os_allowed(spec: ModuleSpec) -> bool:
    if not spec.os_constraints:
        return True
    return _current_os() in spec.os_constraints


def load_enabled_modules(app: FastAPI, cfg: Config) -> list[LoadedModule]:
    """Discover, filter by `enabled_modules` + OS constraints, import, register."""
    registry = get_registry()
    discovered = discover_modules(cfg)
    by_name = {d.spec.name: d for d in discovered}

    loaded: list[LoadedModule] = []
    for name in cfg.enabled_modules:
        d = by_name.get(name)
        if d is None:
            registry.record_error(
                name,
                f"enabled in config.yaml but not discovered in {cfg.modules_dir} "
                f"or bundled modules",
            )
            continue
        if not _os_allowed(d.spec):
            registry.record_error(
                name,
                f"skipped: requires OS {d.spec.os_constraints}, running {_current_os()}",
            )
            continue
        try:
            pkg = _import_module_package(d)
            register = getattr(pkg, "register", None)
            if not callable(register):
                raise RuntimeError(
                    f"module {name!r} has no callable register(app, cfg) in its package"
                )
            exports = register(app, cfg) or {}
            lm = LoadedModule(
                spec=d.spec, source=d.source, root=d.root, package=pkg, exports=dict(exports)
            )
            registry.register(lm)
            loaded.append(lm)
        except Exception as e:  # pragma: no cover — surfaced to UI / logs
            registry.record_error(name, f"{type(e).__name__}: {e}")
    return loaded
