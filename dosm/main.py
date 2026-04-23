from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from dosm import __version__
from dosm.config import Config, load_config

PACKAGE_ROOT = Path(__file__).resolve().parent
TEMPLATES_DIR = PACKAGE_ROOT / "web" / "templates"
STATIC_DIR = PACKAGE_ROOT / "web" / "static"


def create_app(config: Config | None = None) -> FastAPI:
    cfg = config or load_config()

    app = FastAPI(title="DOSM", version=__version__)
    app.state.config = cfg

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "version": __version__,
                "home": str(cfg.home),
                "enabled_modules": cfg.enabled_modules,
                "llm_model": cfg.llm.model,
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
