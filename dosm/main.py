from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func, select, text
from sqlalchemy.orm import Session

from dosm import __version__
from dosm.agent import agent_router
from dosm.auth.deps import (
    _NotAuthenticated,
    get_current_user,
    not_authenticated_exception_handler,
    require_user,
)
from dosm.auth.routes import router as auth_router
from dosm.auth.session import install_session_middleware
from dosm.certs import certs_router
from dosm.certs.scanner import peek_cached as peek_cached_certs
from dosm.config import Config, load_config
from dosm.credentials import credentials_router
from dosm.db import get_session, init_engine
from dosm.docs_index import docs_router
from dosm.docs_index.indexer import reindex_async, warm_embedder_async
from dosm.docs_index.watcher import start_watcher, stop_watcher
from dosm.ftp import ftp_router
from dosm.guacamole import guacamole_router
from dosm.hosts import hosts_router
from dosm.jumps import gc_loop, get_tunnel_manager
from dosm.llm import chat_router
from dosm.metrics import metrics_router
from dosm.models import AuditLog, Host, PipelineRun, User
from dosm.monitoring import monitoring_router
from dosm.network import network_router
from dosm.org import org_router
from dosm.pipelines import pipelines_router
from dosm.pipelines.poller import pipeline_poll_loop
from dosm.recording import recording_router
from dosm.recording.routes import abort_stale_recordings
from dosm.settings import settings_router
from dosm.terminals import terminals_router

PACKAGE_ROOT = Path(__file__).resolve().parent
TEMPLATES_DIR = PACKAGE_ROOT / "web" / "templates"
STATIC_DIR = PACKAGE_ROOT / "web" / "static"


def _humanize_ago(ts: datetime, now: datetime) -> str:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    seconds = int((now - ts).total_seconds())
    if seconds < 60:
        return f"{max(seconds, 0)}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _action_color(action: str) -> str:
    if (
        action.endswith(".delete")
        or action.endswith(".fail")
        or action.endswith(".reject")
    ):
        return "red"
    if (
        action.endswith(".create")
        or action.endswith(".approve")
        or action.endswith(".execute")
        or action.endswith(".start")
        or action == "auth.login"
        or action == "host.connect"
        or action == "pipeline.run"
    ):
        return "green"
    if (
        action.endswith(".update")
        or action.endswith(".partial")
        or action.endswith(".refresh")
    ):
        return "amber"
    return "blue"


def create_app(config: Config | None = None) -> FastAPI:
    cfg = config or load_config()
    init_engine(cfg)
    # Create any new tables and apply idempotent column-add migrations so an
    # older DOSM_HOME upgrades without requiring a manual `dosm db init`.
    from dosm.db import create_all
    create_all(cfg)

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

    @app.middleware("http")
    async def no_cache_html(request, call_next):
        response = await call_next(request)
        ct = response.headers.get("content-type", "")
        if ct.startswith("text/html"):
            response.headers["Cache-Control"] = "no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
        return response

    app.include_router(auth_router)
    app.include_router(credentials_router)
    app.include_router(hosts_router)
    # Always mount /hosts/{id}/connect; the route itself handles the
    # guacamole.enabled=false case with a friendly error page.
    app.include_router(guacamole_router)
    app.include_router(docs_router)
    app.include_router(pipelines_router)
    app.include_router(certs_router)
    app.include_router(monitoring_router)
    # Agent plan card routes share the /chat prefix and must register before
    # the broader chat_router so /chat/{cid}/plan/... matches before any
    # generic /chat/{cid}/... handler that doesn't exist (defensive).
    app.include_router(agent_router)
    app.include_router(chat_router)
    app.include_router(metrics_router)
    app.include_router(org_router)
    app.include_router(network_router)
    app.include_router(ftp_router)
    app.include_router(settings_router)
    app.include_router(recording_router)
    if cfg.terminals.enabled:
        app.include_router(terminals_router)

    @app.on_event("startup")
    async def _warm_and_index() -> None:
        abort_stale_recordings(cfg)
        warm_embedder_async(cfg)
        if cfg.docs_index.auto_index_on_startup:
            reindex_async(cfg, force=False)
        # Watch $DOSM_HOME/docs/ for external file changes (dropped-in files,
        # rsync, etc.) so the RAG index stays current automatically.
        start_watcher(cfg)
        # Reap idle jump SSH connections whose forwards have all been
        # released. Without this, a tab close that misses the pagehide
        # beacon would leak the jump conn until process exit.
        import asyncio
        app.state.tunnel_gc_task = asyncio.create_task(gc_loop())
        if cfg.pipelines.poller_enabled:
            app.state.pipeline_poller_task = asyncio.create_task(pipeline_poll_loop(cfg))

    @app.on_event("shutdown")
    async def _stop_background_tasks() -> None:
        stop_watcher()
        for attr in ("tunnel_gc_task", "pipeline_poller_task"):
            task = getattr(app.state, attr, None)
            if task is not None:
                task.cancel()

    @app.get("/", response_class=HTMLResponse)
    async def index(
        request: Request,
        user: User = Depends(require_user),
        db: Session = Depends(get_session),
    ) -> HTMLResponse:
        now = datetime.now(UTC)
        since_24h = now - timedelta(hours=24)

        host_count = db.execute(select(func.count(Host.id))).scalar_one()
        jumpbox_count = db.execute(
            select(func.count(Host.id)).where(Host.is_jumpbox.is_(True))
        ).scalar_one()
        pipeline_24h_total = db.execute(
            select(func.count(PipelineRun.id)).where(PipelineRun.triggered_at >= since_24h)
        ).scalar_one()
        pipeline_24h_failed = db.execute(
            select(func.count(PipelineRun.id))
            .where(PipelineRun.triggered_at >= since_24h)
            .where(PipelineRun.status.in_(("failed", "cancelled")))
        ).scalar_one()

        cert_cache = peek_cached_certs()
        if cert_cache is not None:
            cert_list, _ = cert_cache
            cert_total = len(cert_list)
            cert_attention = sum(
                1 for c in cert_list if c.status in ("expired", "critical", "warn")
            )
        else:
            cert_total = None
            cert_attention = None

        # Recent activity — last 12 audit log rows joined to actor username.
        activity_rows = db.execute(
            select(AuditLog, User.username)
            .outerjoin(User, AuditLog.actor_id == User.id)
            .order_by(desc(AuditLog.ts))
            .limit(12)
        ).all()
        recent_activity = [
            {
                "ts": entry.ts,
                "ago": _humanize_ago(entry.ts, now),
                "actor": username,
                "action": entry.action,
                "color": _action_color(entry.action),
                "target": entry.target,
                "details": entry.details,
            }
            for entry, username in activity_rows
        ]

        # Recent hosts — distinct host ids most recently opened via Connect.
        recent_target_rows = db.execute(
            select(AuditLog.target, func.max(AuditLog.ts).label("last_ts"))
            .where(AuditLog.action == "host.connect")
            .where(AuditLog.target.like("host:%"))
            .group_by(AuditLog.target)
            .order_by(desc("last_ts"))
            .limit(6)
        ).all()
        host_id_ts: list[tuple[int, datetime]] = []
        for target, last_ts in recent_target_rows:
            try:
                host_id_ts.append((int(target.split(":", 1)[1]), last_ts))
            except (ValueError, IndexError):
                continue
        recent_hosts: list[dict] = []
        if host_id_ts:
            ids = [hid for hid, _ in host_id_ts]
            hosts_by_id = {
                h.id: h
                for h in db.execute(select(Host).where(Host.id.in_(ids))).scalars()
            }
            for hid, ts in host_id_ts:
                h = hosts_by_id.get(hid)
                if h is not None:
                    recent_hosts.append({"host": h, "ago": _humanize_ago(ts, now)})

        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "user": user,
                "version": __version__,
                "host_count": host_count,
                "jumpbox_count": jumpbox_count,
                "pipeline_24h_total": pipeline_24h_total,
                "pipeline_24h_failed": pipeline_24h_failed,
                "cert_total": cert_total,
                "cert_attention": cert_attention,
                "recent_activity": recent_activity,
                "recent_hosts": recent_hosts,
                "guacamole_enabled": cfg.guacamole.enabled,
            },
        )

    @app.get("/health")
    async def health(
        request: Request,
        db: Session = Depends(get_session),
        user: User | None = Depends(get_current_user),
    ) -> Response:
        # Liveness probe: SELECT 1 against the configured DB. Cheap, local,
        # confirms the engine + file is reachable.
        db_ok = True
        db_error: str | None = None
        try:
            db.execute(text("SELECT 1")).scalar_one()
        except Exception as exc:
            db_ok = False
            db_error = str(exc)

        tunnel_stats = get_tunnel_manager().stats()

        overall_ok = db_ok
        payload = {
            "status": "ok" if overall_ok else "degraded",
            "version": __version__,
            "home": str(cfg.home),
            "db_ok": db_ok,
            "db_error": db_error,
            "tunnels": tunnel_stats,
        }

        # Content-negotiate: browsers (Accept includes text/html) get a
        # styled page; everything else (probes, curl, monitoring) gets JSON
        # so existing /health consumers keep working unchanged.
        accept = request.headers.get("accept", "")
        if "text/html" in accept:
            # Browser navigation needs the sidebar shell, which expects a
            # logged-in user. Probes (Accept: */* or application/json) bypass
            # this entirely and get JSON without auth.
            if user is None:
                raise _NotAuthenticated(request.url.path)
            return templates.TemplateResponse(
                request,
                "health.html",
                {
                    "user": user,
                    "overall_ok": overall_ok,
                    "version": __version__,
                    "home": str(cfg.home),
                    "secrets_backend": cfg.secrets.backend,
                    "llm_model": cfg.llm.model,
                    "guacamole_enabled": cfg.guacamole.enabled,
                    "db_ok": db_ok,
                    "db_error": db_error,
                    "tunnel_stats": tunnel_stats,
                    "json_payload": payload,
                },
            )
        return JSONResponse(payload)

    return app
