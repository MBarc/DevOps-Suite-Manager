from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from dosm import __version__
from dosm.auth.deps import (
    _NotAuthenticated,
    not_authenticated_exception_handler,
    require_user,
)
from dosm.auth.routes import router as auth_router
from dosm.auth.session import install_session_middleware
from dosm.config import Config, load_config
from dosm.db import init_engine
from dosm.hosts import hosts_router
from dosm.models import User

PACKAGE_ROOT = Path(__file__).resolve().parent
TEMPLATES_DIR = PACKAGE_ROOT / "web" / "templates"
STATIC_DIR = PACKAGE_ROOT / "web" / "static"


def create_app(config: Config | None = None) -> FastAPI:
    cfg = config or load_config()
    init_engine(cfg)

    app = FastAPI(title="DOSM", version=__version__)
    app.state.config = cfg

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    app.state.templates = templates

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    install_session_middleware(app, cfg)
    app.add_exception_handler(_NotAuthenticated, not_authenticated_exception_handler)

    app.include_router(auth_router)
    app.include_router(hosts_router)

    @app.get("/", response_class=HTMLResponse)
    async def index(
        request: Request,
        user: User = Depends(require_user),
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "version": __version__,
                "home": str(cfg.home),
                "enabled_modules": cfg.enabled_modules,
                "llm_model": cfg.llm.model,
                "user": user,
                "secrets_backend": cfg.secrets.backend,
            },
        )

    @app.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "version": __version__,
                "home": str(cfg.home),
                "modules": cfg.enabled_modules,
            }
        )

    return app
