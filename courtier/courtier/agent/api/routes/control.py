"""Control routes — pause, resume, stop agent runs."""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..middleware.auth import _is_admin, get_current_user
from ..rate_limiter import limiter

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/pause")
@limiter.limit("10/minute")
async def pause_session(
    request: Request,
    current_user_payload: dict = Depends(get_current_user),
):
    """Pause the currently running session (admin only)."""
    if not _is_admin(current_user_payload):
        raise HTTPException(403, "仅管理员可暂停全部会话")
    pause_event = request.app.state.pause_event
    pause_event.set()
    return {"status": "ok"}


@router.post("/resume")
@limiter.limit("10/minute")
async def resume_session(
    request: Request,
    current_user_payload: dict = Depends(get_current_user),
):
    """Resume a paused session (admin only)."""
    if not _is_admin(current_user_payload):
        raise HTTPException(403, "仅管理员可恢复全部会话")
    pause_event = request.app.state.pause_event
    pause_event.clear()
    return {"status": "ok"}


@router.post("/stop")
@limiter.limit("10/minute")
async def stop_session(
    request: Request,
    sessionId: Optional[str] = Query(default=None),
    current_user_payload: dict = Depends(get_current_user),
):
    """Stop a running session (cancels its background run).

    If sessionId is provided, only that session is stopped and the caller
    must own the session or be an admin. If omitted, all active sessions
    are stopped (admin only).
    """
    run_manager = request.app.state.run_manager
    is_admin = _is_admin(current_user_payload)
    current_user = current_user_payload["sub"]

    if sessionId:
        session_store = request.app.state.session_store
        owned = await session_store.get_owned(sessionId, current_user, is_admin)
        if owned is None:
            raise HTTPException(404, f"会话 {sessionId} 未找到或无权限")

        # Cancel in-flight plugin requests before cancelling the agent task.
        # This sends request.cancel notifications so plugins stop processing.
        # Permission is verified first: this cancels plugin-wide work.
        plugin_system = getattr(request.app.state, "plugin_system", None)
        if plugin_system is not None:
            await plugin_system.cancel_pending()

        stopped = await run_manager.stop(sessionId)
        if not stopped:
            raise HTTPException(404, f"会话 {sessionId} 未在运行")
        return {"status": "ok", "sessionId": sessionId, "stopped": True}

    # Stop all active sessions (admin only)
    if not is_admin:
        raise HTTPException(403, "仅管理员可停止全部会话")

    # Cancel in-flight plugin requests before cancelling the agent tasks.
    plugin_system = getattr(request.app.state, "plugin_system", None)
    if plugin_system is not None:
        await plugin_system.cancel_pending()

    stopped = await run_manager.stop_all()
    if not stopped:
        return {"status": "ok", "stopped": 0, "message": "没有正在运行的任务"}

    return {"status": "ok", "stoppedCount": len(stopped), "sessionIds": stopped}
