"""Control routes — pause, resume, stop agent runs."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..middleware.auth import get_current_user
from ..rate_limiter import limiter

router = APIRouter()
logger = logging.getLogger(__name__)


def _is_admin(payload: dict) -> bool:
    return payload.get("role") == "admin"


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
    """Stop a running session by cancelling its agent task.

    If sessionId is provided, only that session is stopped and the caller
    must own the session or be an admin. If omitted, all active sessions
    are stopped (admin only).
    """
    active_tasks: dict[str, asyncio.Task] = request.app.state.active_tasks
    is_admin = _is_admin(current_user_payload)
    current_user = current_user_payload["sub"]

    # Cancel in-flight plugin requests before cancelling the agent task.
    # This sends request.cancel notifications so plugins stop processing.
    plugin_system = getattr(request.app.state, "plugin_system", None)
    if plugin_system is not None:
        await plugin_system.cancel_pending()

    if sessionId:
        session_store = request.app.state.session_store
        owned = await session_store.get_owned(
            sessionId, current_user, is_admin
        )
        if owned is None:
            raise HTTPException(404, f"会话 {sessionId} 未找到或无权限")

        task = active_tasks.get(sessionId)
        if task is None:
            raise HTTPException(404, f"会话 {sessionId} 未在运行")
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except asyncio.CancelledError:
            pass
        except asyncio.TimeoutError:
            logger.warning("Timeout awaiting cancellation for session %s; forcing cleanup", sessionId)
            active_tasks.pop(sessionId, None)
        except Exception:
            logger.exception("Error awaiting cancellation for session %s", sessionId)
        return {"status": "ok", "sessionId": sessionId, "stopped": True}

    # Stop all active sessions (admin only)
    if not is_admin:
        raise HTTPException(403, "仅管理员可停止全部会话")

    if not active_tasks:
        return {"status": "ok", "stopped": 0, "message": "没有正在运行的任务"}

    stopped = []
    for sid, task in list(active_tasks.items()):
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            logger.warning("Timeout awaiting cancellation for session %s; forcing cleanup", sid)
            active_tasks.pop(sid, None)
        except Exception:
            logger.exception("Error awaiting cancellation for session %s", sid)
        stopped.append(sid)

    return {"status": "ok", "stoppedCount": len(stopped), "sessionIds": stopped}
