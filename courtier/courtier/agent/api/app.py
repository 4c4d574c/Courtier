"""FastAPI application for Courtier API."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from starlette.responses import Response

from courtier.agent.telemetry.tracer import init_telemetry

from ...agent.artifacts.registry import SessionArtifactStoreRegistry
from ...agent.artifacts.store import ArtifactStore
from ..tools.registry import ToolRegistry
from .file_store import FileStore
from .middleware.observability import ObservabilityMiddleware
from .rate_limiter import limiter
from .routes import router as api_router
from .routes.admin_users import router as admin_router
from .routes.auth import router as auth_router
from .routes.profile import router as profile_router
from .session_store import SessionStore

logger = logging.getLogger(__name__)


def create_app(sessions_dir: str = "", start_plugins: bool = True) -> FastAPI:
    """Create and configure the FastAPI application.

    Args:
        sessions_dir: Directory for session storage.
        start_plugins: If False, skip PluginSystem.start() at startup.
                       Set to False in test suites that don't need plugins.
    """
    from courtier.config import Settings

    settings = Settings()

    # Configure structured logging before anything else so all subsequent
    # imports and module-level log statements are captured.
    from ..core.logging_config import configure_logging

    configure_logging(
        log_dir=settings.audit_log_dir,
        level=settings.logger_level,
        console=True,
    )

    from courtier.plugin import PluginSystem

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Startup: start PluginSystem. Shutdown: stop PluginSystem."""
        if start_plugins:
            await app.state.plugin_system.start()
        init_telemetry()
        from .db import bootstrap_admin_user, get_db

        # DB-less mode (empty MYSQL_URL) is a supported configuration for
        # tests and local dev — skip engine creation just like
        # bootstrap_admin_user skips its own DB work in that mode.
        if settings.mysql_url:
            db = get_db()
            await db.ensure_database()
        await bootstrap_admin_user()
        # Ensure the ES chunks index exists (init_index is a no-op when it
        # does).  ES-less mode (empty es_hosts) skips this, and an
        # unreachable cluster only logs a warning instead of aborting
        # startup — search tools will report the error when actually used.
        if settings.es_hosts:
            from courtier.es import init_index

            try:
                await asyncio.to_thread(init_index)
            except Exception:
                logger.warning(
                    "Elasticsearch index initialization failed; search features "
                    "will fail until the cluster is reachable",
                    exc_info=True,
                )
        try:
            yield
        finally:
            if start_plugins:
                await app.state.plugin_system.shutdown()

    app = FastAPI(
        title="Courtier API",
        description="General-purpose AI agent platform with pluggable domain packages",
        version="0.2.0",
        lifespan=lifespan,
    )

    # Security headers middleware
    @app.middleware("http")
    async def add_security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        if settings.deployment_env == "production":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["Content-Security-Policy"] = "default-src 'self'; frame-ancestors 'none';"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        return response

    # CORS — configured via Settings
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=settings.cors_allow_credentials and settings.cors_origins != ["*"],
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )
    app.add_middleware(ObservabilityMiddleware)

    # Rate limiting
    app.state.limiter = limiter
    # Starlette dispatches exception handlers by exception class, so the
    # slowapi handler's narrower RateLimitExceeded parameter is safe here.
    app.add_exception_handler(
        RateLimitExceeded,
        _rate_limit_exceeded_handler,  # type: ignore[arg-type]
    )

    # Shared state
    app.state.settings = settings
    app.state.session_store = SessionStore(
        sessions_dir or str(Path(settings.cache_dir) / "sessions")
    )
    app.state.file_store = FileStore(str(Path(settings.upload_dir) / ".file_registry"))
    app.state.pause_event = asyncio.Event()
    app.state.active_tasks = {}
    app.state.tool_registry = ToolRegistry()

    # Unified artifact store — replaces the old separate CacheStore and
    # ArtifactStore.  Handles disk persistence, $ref resolution, and typed
    # artifact registration in one place.
    app.state.artifact_store = ArtifactStore(
        cache_dir=settings.cache_dir,
        large_output_threshold=(
            settings.llm_large_output_threshold
            if hasattr(settings, "llm_large_output_threshold")
            else 3000
        ),
        preview_max_chars=getattr(settings, "context_preview_max_chars", 1000),
    )
    app.state.artifact_store_registry = SessionArtifactStoreRegistry()

    # Load PromptBundle and create PromptEngine via CourtierConfig
    from courtier.config import CourtierConfig

    # Resolve repo root from this file's location rather than CWD
    # app.py is at: courtier/agent/api/app.py (4 levels from repo root)
    _repo_root = Path(__file__).resolve().parent.parent.parent.parent
    courtier_config = CourtierConfig.from_env(repo_root=_repo_root)
    courtier_config.discover()  # validates all domains exist
    app.state.courtier_config = courtier_config
    app.state.prompt_engine = courtier_config.build_prompt_engine()

    # PluginSystem scans the first domain's plugins dir
    # (multi-domain plugin merging is a future enhancement)
    primary_domain = courtier_config.domains[0]
    app.state.plugin_system = PluginSystem(
        plugins_dir=str(primary_domain.plugins_path),
        tool_registry=app.state.tool_registry,
        artifact_store=app.state.artifact_store,
        artifact_store_registry=app.state.artifact_store_registry,
        log_dir=str(Path(settings.audit_log_dir) / "plugins"),
    )

    # Plugin names living under plugins/shared/ — chat-mode agents get these
    # tools (domain audit plugins stay audit-mode only).
    shared_plugins_dir = courtier_config.repo_root / "plugins" / "shared"
    app.state.shared_plugin_names = (
        {
            p.name
            for p in shared_plugins_dir.iterdir()
            if p.is_dir() and (p / "plugin.yaml").exists()
        }
        if shared_plugins_dir.is_dir()
        else set()
    )

    # Routes
    # /metrics is Prometheus-only and optional; protect with a shared
    # secret token when PROMETHEUS_METRICS_TOKEN is set in the environment.
    @app.get("/metrics")
    async def metrics(request: Request):
        """Prometheus metrics endpoint — optionally token-protected."""
        expected = os.getenv("PROMETHEUS_METRICS_TOKEN", "").strip()
        if expected:
            token = request.query_params.get("token", "")
            if token != expected:
                return Response("Unauthorized", status_code=401)
        return Response(
            generate_latest(),
            media_type=CONTENT_TYPE_LATEST,
        )

    # Auth routes — public (no JWT required)
    app.include_router(auth_router)

    # Health check — no auth, used by Docker HEALTHCHECK and load balancers
    @app.get("/health")
    async def health():
        return {"status": "ok"}

    # API routes — JWT-protected via Depends in the router
    app.include_router(api_router)

    # Admin routes — JWT + admin-role protected
    app.include_router(admin_router)

    # Profile routes — JWT-protected
    app.include_router(profile_router)

    # Resource library routes — JWT-protected (delete is admin-only)
    from .routes.resources import router as resources_router

    app.include_router(resources_router)

    # Admin extension management routes — admin-only
    from .routes.admin_extensions import router as admin_extensions_router

    app.include_router(admin_extensions_router)

    # Serve built frontend static assets in production/Docker images.
    # The Dockerfile copies webui/dist to $COURTIER_REPO_ROOT/static.
    static_dir = Path(os.environ.get("COURTIER_REPO_ROOT", ".")) / "static"
    if static_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

    return app
