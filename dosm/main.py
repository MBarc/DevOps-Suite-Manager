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
from dosm.agent import agent_router
from dosm.docs_index import docs_router
from dosm.docs_index.indexer import reindex_async, warm_embedder_async
from dosm.llm import chat_router
from dosm.hosts import hosts_router
from dosm.metrics import metrics_router
from dosm.models import User
from dosm.modules.loader import load_enabled_modules
from dosm.modules.registry import get_registry
from dosm.modules.routes import router as modules_router
from dosm.terminals import terminals_router

PACKAGE_ROOT = Path(__file__).resolve().parent
TEMPLATES_DIR = PACKAGE_ROOT / "web" / "templates"
STATIC_DIR = PACKAGE_ROOT / "web" / "static"


def create_app(config: Config | None = None) -> FastAPI:
    cfg = config or load_config()
    init_engine(cfg)

    app = FastAPI(
        title="DOSM",
        version=__version__,
        # Move Swagger/ReDoc off /docs so the documentation index owns that URL.
        docs_url="/_api/docs",
        redoc_url="/_api/redoc",
        openapi_url="/_api/openapi.json",
    )
    app.state.config = cfg

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    app.state.templates = templates

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    install_session_middleware(app, cfg)
    app.add_exception_handler(_NotAuthenticated, not_authenticated_exception_handler)

    app.include_router(auth_router)
    app.include_router(hosts_router)
    app.include_router(docs_router)
    # Agent plan card routes share the /chat prefix and must register before
    # the broader chat_router so /chat/{cid}/plan/... matches before any
    # generic /chat/{cid}/... handler that doesn't exist (defensive).
    app.include_router(agent_router)
    app.include_router(chat_router)
    app.include_router(modules_router)
    app.include_router(metrics_router)
    if cfg.terminals.enabled:
        app.include_router(terminals_router)

    load_enabled_modules(app, cfg)

    @app.on_event("startup")
    async def _warm_and_index() -> None:
        # Warm the embedder in a background thread so no request ever pays the
        # first-time init cost (HF download, ONNX load).
        warm_embedder_async(cfg)
        if cfg.docs_index.auto_index_on_startup:
            reindex_async(cfg, force=False)

    @app.get("/", response_class=HTMLResponse)
    async def index(
        request: Request,
        user: User = Depends(require_user),
    ) -> HTMLResponse:
        reg = get_registry()
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "version": __version__,
                "home": str(cfg.home),
                "enabled_modules": cfg.enabled_modules,
                "loaded_modules": reg.loaded(),
                "module_errors": reg.errors(),
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
