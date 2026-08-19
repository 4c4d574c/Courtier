"""Global events channel — per-user run-status push (GET /api/events)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from ..middleware.auth import get_current_user
from ..rate_limiter import limiter

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/events")
@limiter.limit("10/minute")
async def global_events(
    request: Request,
    current_user_payload: dict = Depends(get_current_user),
):
    """Push this user's run-status transitions (queued/started/completed/
    error/stopped) as they happen. No replay — the client re-fetches the
    session list after a reconnect to close any gap."""
    hub = request.app.state.notification_hub
    conn = hub.subscribe(current_user_payload["sub"])
    return StreamingResponse(
        hub.stream(conn),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
