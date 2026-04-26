from __future__ import annotations

from threading import RLock

from dosm.modules.contract import DiscoveredModule, LoadedModule


class ModuleRegistry:
    """Process-wide registry of discovered and loaded modules."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._discovered: dict[str, DiscoveredModule] = {}
        self._loaded: dict[str, LoadedModule] = {}
        self._errors: dict[str, str] = {}

    # Discovery --------------------------------------------------------------

    def set_discovered(self, items: list[DiscoveredModule]) -> None:
        with self._lock:
            self._discovered = {d.spec.name: d for d in items}

    def discovered(self) -> list[DiscoveredModule]:
        with self._lock:
            return list(self._discovered.values())

    def get_discovered(self, name: str) -> DiscoveredModule | None:
        with self._lock:
            return self._discovered.get(name)

    # Loaded -----------------------------------------------------------------

    def register(self, module: LoadedModule) -> None:
        with self._lock:
            self._loaded[module.spec.name] = module
            self._errors.pop(module.spec.name, None)

    def record_error(self, name: str, message: str) -> None:
        with self._lock:
            self._errors[name] = message

    def loaded(self) -> list[LoadedModule]:
        with self._lock:
            return list(self._loaded.values())

    def get(self, name: str) -> LoadedModule | None:
        with self._lock:
            return self._loaded.get(name)

    def errors(self) -> dict[str, str]:
        with self._lock:
            return dict(self._errors)


_registry: ModuleRegistry | None = None


def get_registry() -> ModuleRegistry:
    global _registry
    if _registry is None:
        _registry = ModuleRegistry()
    return _registry


def reset_registry_for_tests() -> None:
    global _registry
    _registry = ModuleRegistry()
