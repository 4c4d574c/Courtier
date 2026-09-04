"""FastAPI application for Courtier API."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

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
from .services.notification_hub import NotificationHub
from .services.run_manager import RunManager
from .session_store import SessionStore

logger = logging.getLogger(__name__)


def _bind_dynamic_settings(app: FastAPI, service: Any = None) -> Any:
    """Keep app.state.settings pointing at the CURRENT ConfigService snapshot.

    Returns the unsubscribe callable.  Snapshot replacement (DB-backed
    settings at startup and on admin saves) must reach every per-request
    consumer that reads ``request.app.state.settings``; a direct reference
    assigned once at factory time would keep pre-replacement values (empty
    jwt_secret after the env→DB migration broke login with
    InvalidKeyError)."""
    from courtier.config import get_config_service

    service = service or get_config_service()

    def _on_settings_changed(new_settings: Any, _version: int) -> None:
        app.state.settings = new_settings

    return service.subscribe(_on_settings_changed)


def create_app(sessions_dir: str = "", start_plugins: bool = True) -> FastAPI:
    """Create and configure the FastAPI application.

    Args:
        sessions_dir: Directory for session storage.
        start_plugins: If False, skip PluginSystem.start() at startup.
                       Set to False in test suites that don't need plugins.
    """
    from courtier.config import get_settings

    settings = get_settings()

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
        """Startup order matters:

        1. DB engine + admin-existence flag (Tier 0).
        2. DB-backed settings snapshot swap — everything below must read
           DB-sourced values (plugin endpoints/token, OTel, ES hosts,
           log level).  Starting plugins before this point read the
           env-only snapshot and BLOCKED every plugin terminally after
           the env→DB migration of COURTIER_PLUGIN_ENDPOINTS.
        3. Plugins, telemetry, stale-session sweep, ES index init.
        """
        from .db import get_db

        # DB-less mode (empty MYSQL_URL) is a supported configuration for
        # tests and local dev — skip engine creation in that mode.
        if settings.mysql_url:
            db = get_db()
            await db.ensure_database()
        # Setup gate flag: an admin existing anywhere (a previous setup
        # wizard run) opens the production API surface.
        if settings.mysql_url:
            from .setup_gate import admin_exists

            try:
                app.state._has_admin = await admin_exists()
            except Exception:
                logger.warning("admin existence check failed; gate stays closed", exc_info=True)
                app.state._has_admin = False
        # DB-backed settings: compose env base + DB overrides into the
        # effective snapshot (first-run .env seeding, JWT bootstrap and
        # env-only degradation handled inside).  app.state.settings_store
        # backs the admin settings API; the subscription installed at
        # factory time rebinds app.state.settings to the new snapshot.
        if settings.mysql_url:
            from courtier.config import get_config_service
            from courtier.settings_store import (
                FernetCodec,
                SettingsStore,
                refresh_settings_snapshot,
            )

            app.state.settings_store = SettingsStore(get_db(), FernetCodec.from_env())
            info = await refresh_settings_snapshot(
                get_config_service(), app.state.settings_store
            )
            logger.info(
                "settings snapshot: mode=%s version=%s seeded=%d unreadable=%d",
                info["mode"],
                info["version"],
                len(info["seeded"]),
                len(info["unreadable"]),
            )
            # The factory configured logging from env-only values; re-apply
            # with the effective snapshot so a DB-backed logger_level holds.
            from ..core.logging_config import configure_logging

            effective = get_settings()
            configure_logging(
                log_dir=effective.audit_log_dir,
                level=effective.logger_level,
                console=True,
            )
        else:
            app.state.settings_store = None

        if start_plugins:
            await app.state.plugin_system.start()
        init_telemetry()
        # Runs are in-process only: any persisted "running" session died with
        # the previous process — mark it interrupted so it never shows as
        # perpetually running.
        await app.state.run_manager.sweep_stale_sessions()
        # Ensure the ES chunks index exists (init_index is a no-op when it
        # does).  ES-less mode (empty es_hosts) skips this, and an
        # unreachable cluster only logs a warning instead of aborting
        # startup — search tools will report the error when actually used.
        if get_settings().es_hosts:
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
            # Cancel any still-running background sessions before teardown.
            await app.state.run_manager.shutdown()
            # Detach config listeners (app.state.settings rebind + run
            # manager limits) so a discarded app cannot leak them.
            for handle_name in ("_settings_unsubscribe", "_config_unsubscribe"):
                unsubscribe = getattr(app.state, handle_name, None)
                if callable(unsubscribe):
                    unsubscribe()
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

    # Production first-run gate (no-op in development / env-only mode).
    from .setup_gate import install_setup_gate

    install_setup_gate(app, settings)

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
    # Per-request consumers read request.app.state.settings (auth middleware,
    # routes).  A direct reference assigned here would go STALE when the
    # DB-backed settings replace the ConfigService snapshot at startup or on
    # admin saves — e.g. jwt_secret moving from env to the DB left the old
    # reference empty and broke login with InvalidKeyError.  Re-bind it on
    # every snapshot replacement.
    app.state._settings_unsubscribe = _bind_dynamic_settings(app)
    app.state.session_store = SessionStore(
        sessions_dir or str(Path(settings.cache_dir) / "sessions")
    )
    app.state.file_store = FileStore(str(Path(settings.upload_dir) / ".file_registry"))
    app.state.pause_event = asyncio.Event()
    app.state.notification_hub = NotificationHub()
    app.state.run_manager = RunManager(
        session_store=app.state.session_store,
        settings=settings,
        pause_event=app.state.pause_event,
    )
    app.state.run_manager.set_notification_hub(app.state.notification_hub)
    # Run-limit knobs (and later per-run reads) follow configuration
    # changes: Phase 0 wiring — the env-only ConfigService never fires,
    # the DB-backed phases drive it.  Keep the unsubscribe handle so a
    # future shutdown path can detach cleanly.
    from courtier.config import get_config_service

    app.state._config_unsubscribe = get_config_service().subscribe(
        lambda new_settings, _version: app.state.run_manager.apply_settings(new_settings)
    )
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

    # Host-side tool-boundary glue: the embedding param injector fills
    # host-injected tool parameters (e.g. the hybrid-search query vector)
    # after $ref resolution and before dispatch; the search rerank
    # post-processor reorders coarse search candidates with the main model
    # and sends them back through the plugin's finalize pass (pagination +
    # neighbor expansion) before persistence.
    from courtier.agent.runtime.search_rerank import make_search_rerank_post_processor
    from courtier.agent.tools.param_injection import make_embedding_param_injector

    app.state.tool_registry.configure_param_injectors(
        {"embedding": make_embedding_param_injector()}
    )
    app.state.tool_registry.configure_result_post_processors(
        [make_search_rerank_post_processor(app.state.prompt_engine, app.state.tool_registry)]
    )

    # PluginSystem scans the first domain's plugins dir for manifests
    # (multi-domain plugin merging is a future enhancement) and dials the
    # standalone plugin servers listed in COURTIER_PLUGIN_ENDPOINTS.
    primary_domain = courtier_config.domains[0]
    app.state.plugin_system = PluginSystem(
        plugins_dir=str(primary_domain.plugins_path),
        tool_registry=app.state.tool_registry,
        artifact_store=app.state.artifact_store,
        artifact_store_registry=app.state.artifact_store_registry,
        # memory_store host service validates entries against the loaded
        # domain packages (live read — domains can be added without restart).
        domain_names_provider=lambda: {pkg.name for pkg in courtier_config.domains},
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
        from courtier.config import get_config_service

        service = get_config_service()
        has_admin = getattr(app.state, "_has_admin", None)
        return {
            "status": "ok",
            "config": {
                "mode": service.source,
                "setup_required": bool(settings.mysql_url) and has_admin is False,
            },
        }

    # API routes — JWT-protected via Depends in the router
    app.include_router(api_router)

    # Admin routes — JWT + admin-role protected
    app.include_router(admin_router)

    # Admin settings (DB-backed configuration surface)
    from .routes.admin_settings import router as admin_settings_router

    app.include_router(admin_settings_router)

    # First-run setup wizard (public; the production setup gate only
    # allows /health, /api/setup* and the static SPA until an admin exists)
    from .routes.setup import router as setup_router

    app.include_router(setup_router)

    # Profile routes — JWT-protected
    app.include_router(profile_router)

    # Resource library routes — JWT-protected (delete is admin-only)
    from .routes.resources import router as resources_router

    app.include_router(resources_router)

    # Memory routes — layered DB-backed memory (global read-all, write admin;
    # user layer owner-only) + admin audit trail
    from .routes.memory import admin_router as memory_admin_router
    from .routes.memory import router as memory_router

    app.include_router(memory_router)
    app.include_router(memory_admin_router)

    # Account deletion — self-service requests (password + admin approval)
    # and admin direct deletion with the cascading cleanup pipeline
    from .routes.account_deletion import (
        admin_router as deletion_admin_router,
        admin_users_router as deletion_admin_users_router,
        router as deletion_router,
    )

    app.include_router(deletion_router)
    app.include_router(deletion_admin_router)
    app.include_router(deletion_admin_users_router)

    # Admin extension management routes — admin-only
    from .routes.admin_extensions import router as admin_extensions_router

    app.include_router(admin_extensions_router)

    # Serve built frontend static assets in production/Docker images.
    # The Dockerfile copies webui/dist to $COURTIER_REPO_ROOT/static.
    static_dir = Path(os.environ.get("COURTIER_REPO_ROOT", ".")) / "static"
    if static_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

    return app
