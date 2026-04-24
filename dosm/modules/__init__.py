"""Module system: discover, load, and register DOSM integration modules.

Public API — other code uses:

    from dosm.modules import (
        ModuleSpec, LoadedModule, ModuleRegistry, get_registry,
        discover_modules, load_enabled_modules,
    )
"""
from dosm.modules.contract import LoadedModule, ModuleSpec
from dosm.modules.loader import discover_modules, load_enabled_modules
from dosm.modules.registry import ModuleRegistry, get_registry

__all__ = [
    "LoadedModule",
    "ModuleSpec",
    "ModuleRegistry",
    "discover_modules",
    "get_registry",
    "load_enabled_modules",
]
