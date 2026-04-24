from __future__ import annotations

from fastapi import FastAPI
from jinja2 import ChoiceLoader, FileSystemLoader

from dosm.config import Config
from dosm.modules.builtin.system_info.routes import build_router

MODULE_DIR = __file__.rsplit("/", 1)[0] if "/" in __file__ else __file__.rsplit("\\", 1)[0]


def register(app: FastAPI, cfg: Config) -> dict:
    templates = app.state.templates
    # Extend Jinja's search path so this module's templates are findable.
    existing = templates.env.loader
    module_loader = FileSystemLoader(f"{MODULE_DIR}/templates")
    if isinstance(existing, ChoiceLoader):
        existing.loaders = [*existing.loaders, module_loader]
    else:
        templates.env.loader = ChoiceLoader([existing, module_loader])

    app.include_router(build_router(), prefix="/modules/system_info")
    return {"routes": ["/modules/system_info", "/modules/system_info/api/snapshot"]}
