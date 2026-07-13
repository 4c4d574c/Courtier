"""Control routes — pause, resume, stop agent runs."""

from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request

router = APIRouter()


@router.post("/pause")
async def pause_session(request: Request):
    """Pause the currently running session."""
    pause_event = request.app.state.pause_event
    pause_event.set()
    return {"status": "ok"}


@router.post("/resume")
async def resume_session(request: Request):
    """Resume a paused session."""
    pause_event = request.app.state.pause_event
    pause_event.clear()
    return {"status": "ok"}


@router.post("/stop")
async def stop_session(
    request: Request,
    sessionId: Optional[str] = Query(default=None),
):
    """Stop a running session by cancelling its agent task.

    If sessionId is provided, only that session is stopped.
    If omitted, all active sessions are stopped.
    """
    active_tasks: dict[str, asyncio.Task] = request.app.state.active_tasks

    # Cancel in-flight plugin requests before cancelling the agent task.
    # This sends request.cancel notifications so plugins stop processing.
    plugin_system = getattr(request.app.state, "plugin_system", None)
    if plugin_system is not None:
        await plugin_system.cancel_pending()

    if sessionId:
        task = active_tasks.get(sessionId)
        if task is None:
            raise HTTPException(404, f"会话 {sessionId} 未在运行")
        task.cancel()
        return {"status": "ok", "sessionId": sessionId, "stopped": True}

    # Stop all active sessions
    if not active_tasks:
        return {"status": "ok", "stopped": 0, "message": "没有正在运行的任务"}

    stopped = []
    for sid, task in list(active_tasks.items()):
        task.cancel()
        stopped.append(sid)

    return {"status": "ok", "stoppedCount": len(stopped), "sessionIds": stopped}
