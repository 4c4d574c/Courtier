"""Production first-run gate.

In staging/production with a database configured, every ``/api/*`` route
except ``/health`` and ``/api/setup*`` answers 403 ``setup_required``
until an admin user exists — a fresh deployment cannot be used (or
squatted) before the setup wizard completes.  Non-API paths (static SPA,
docs) stay reachable so the wizard itself can load.

The admin-existence flag lives on ``app.state._has_admin``: computed once
at startup, flipped to True by the setup endpoint after it creates the
admin.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


async def admin_exists() -> bool:
    """True when at least one admin user exists in the users table."""
    from sqlalchemy import select

    from courtier.db.tables.user import UserRole, UserTable

    from .db import get_db

    db = get_db()
    async with db.session() as session:
        row = (
            await session.execute(
                select(UserTable.id).where(UserTable.role == UserRole.admin).limit(1)
            )
        ).scalar()
        return row is not None


def install_setup_gate(app: FastAPI, settings: Any) -> None:
    """Attach the setup gate as an HTTP middleware (dev mode: no-op)."""

    @app.middleware("http")
    async def setup_gate(request: Request, call_next):
        if (
            settings.deployment_env in ("staging", "production")
            and settings.mysql_url
            and not getattr(app.state, "_has_admin", None)
        ):
            path = request.url.path
            allowed = (
                path == "/health"
                or path.startswith("/api/setup")
                or not path.startswith("/api")
            )
            if not allowed:
                return JSONResponse({"detail": "setup_required"}, status_code=403)
        return await call_next(request)
