"""Session routes — list, get, delete, and SSE streaming."""

from __future__ import annotations

import logging
import secrets
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from ..middleware.auth import get_current_user
from ..rate_limiter import limiter
from ..services.agent_service import build_audit_agent, build_chat_agent
from ..services.session_service import list_sessions, get_session, delete_session
from ..services.stream_service import generate_sse_stream, reconstruct_state

logger = logging.getLogger(__name__)

router = APIRouter()


def _is_admin(payload: dict) -> bool:
    return payload.get("role") == "admin"


async def _resolve_audit_file_or_404(
    file_store: Any,
    file_id: str,
    upload_dir: str,
) -> Path:
    """Resolve a file_id to an existing path or raise HTTPException(404)."""
    file_path = await file_store.resolve_path(file_id, upload_dir)
    if file_path is None or not file_path.exists():
        raise HTTPException(404, f"文件不存在: {file_id}")
    return file_path


async def _build_agent_or_500(
    builder_fn: Callable[[], Awaitable[tuple[Any, Any, str]]],
    session_id: str,
    agent_type: str,
) -> tuple[Any, Any, str]:
    """Wrap agent construction and translate failures to HTTPException(500)."""
    try:
        return await builder_fn()
    except Exception:
        logger.exception(
            "Failed to build %s agent for session %s",
            agent_type,
            session_id,
        )
        raise HTTPException(500, "服务器内部错误，请稍后重试")


@router.get("/sessions")
@limiter.limit("10/minute")
async def handle_sessions(
    request: Request,
    task: Optional[str] = Query(default=None),
    fileId: Optional[str] = Query(default=None),
    sessionId: Optional[str] = Query(default=None),
    current_user_payload: dict = Depends(get_current_user),
):
    """List all sessions, create a new one, or continue an existing one.

    - No params: list all historical sessions.
    - task (+ optional fileId), no sessionId: new session (emits session SSE event).
    - task + sessionId: continue existing multi-turn session.
    """
    settings = request.app.state.settings
    session_store = request.app.state.session_store
    current_user = current_user_payload["sub"]
    is_admin = _is_admin(current_user_payload)

    # List mode: no query params
    if task is None and fileId is None and sessionId is None:
        return await list_sessions(session_store, current_user, is_admin)

    # Both modes need at least a task
    if not task:
        raise HTTPException(400, "task 参数必须提供")

    pause_event = request.app.state.pause_event
    active_tasks = request.app.state.active_tasks
    tool_registry = request.app.state.tool_registry

    # Determine mode: new session vs continue existing
    if sessionId:
        # Continue existing session
        existing = await session_store.get_owned(
            sessionId, current_user, is_admin
        )
        if existing is None:
            raise HTTPException(404, "Session not found")

        session_id = sessionId
        prior_state = reconstruct_state(existing.messages_json)
        start_step = len(existing.steps)

        # Record the new turn boundary so historical sessions render
        # each turn with the correct user message and step grouping.
        await session_store.add_turn(sessionId, task)

        if existing.file_id:
            # Audit mode continuation
            file_store = request.app.state.file_store
            file_path = await _resolve_audit_file_or_404(
                file_store, existing.file_id, settings.upload_dir
            )
            agent, context_manager, model_name = await _build_agent_or_500(
                lambda: build_audit_agent(
                    settings,
                    plugin_system=request.app.state.plugin_system,
                    tool_registry=request.app.state.tool_registry,
                    cache_store=request.app.state.cache_store,
                    skills_dir=str(request.app.state.courtier_config.domains[0].skills_path),
                    prompt_engine=request.app.state.prompt_engine,
                ),
                session_id,
                "audit",
            )
            agent_context = {"file_path": str(file_path)}
        else:
            # Chat mode continuation
            agent, context_manager, model_name = await _build_agent_or_500(
                lambda: build_chat_agent(settings, cache_store=request.app.state.cache_store, prompt_engine=request.app.state.prompt_engine),
                session_id,
                "chat",
            )
            agent_context = {}

        is_new = False
    else:
        # New session
        session_id = f"sess_{secrets.token_hex(6)}"
        prior_state = None
        start_step = 0

        if fileId:
            # Document audit mode
            file_store = request.app.state.file_store
            file_path = await _resolve_audit_file_or_404(
                file_store, fileId, settings.upload_dir
            )
            file_info = await file_store.resolve(fileId)
            file_name = file_info.original_name if file_info else ""

            agent, context_manager, model_name = await _build_agent_or_500(
                lambda: build_audit_agent(
                    settings,
                    plugin_system=request.app.state.plugin_system,
                    tool_registry=request.app.state.tool_registry,
                    cache_store=request.app.state.cache_store,
                    skills_dir=str(request.app.state.courtier_config.domains[0].skills_path),
                    prompt_engine=request.app.state.prompt_engine,
                ),
                session_id,
                "audit",
            )
            agent_context = {"file_path": str(file_path)}
        else:
            # Chat mode
            file_name = ""
            agent, context_manager, model_name = await _build_agent_or_500(
                lambda: build_chat_agent(settings, cache_store=request.app.state.cache_store, prompt_engine=request.app.state.prompt_engine),
                session_id,
                "chat",
            )
            agent_context = {}

        is_new = True

    # Create or update session record
    if is_new:
        await session_store.create(
            session_id=session_id,
            task=task,
            file_id=fileId or "",
            file_name=file_name,
            model_name=model_name,
            owner=current_user,
        )
    else:
        await session_store.update(session_id, status="running", error_detail=None)

    return StreamingResponse(
        generate_sse_stream(
            agent=agent,
            session_id=session_id,
            task=task,
            agent_context=agent_context,
            session_store=session_store,
            settings=settings,
            prior_state=prior_state,
            is_new=is_new,
            start_step=start_step,
            context_manager=context_manager,
            model_name=model_name,
            pause_event=pause_event,
            active_tasks=active_tasks,
            audit_base_dir=settings.audit_log_dir,
            audit_log_enabled=settings.audit_log_enabled,
            tool_registry=tool_registry,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/sessions/{session_id}")
async def get_session_detail(
    session_id: str, request: Request, current_user_payload: dict = Depends(get_current_user)
):
    """Get full detail for a historical session."""
    session_store = request.app.state.session_store
    result = await get_session(
        session_store,
        session_id,
        current_user_payload["sub"],
        _is_admin(current_user_payload),
    )
    if result is None:
        raise HTTPException(404, "Session not found")
    return result


@router.delete("/sessions/{session_id}")
async def delete_session_handler(
    session_id: str, request: Request, current_user_payload: dict = Depends(get_current_user)
):
    """Delete a historical session."""
    session_store = request.app.state.session_store
    if not await delete_session(
        session_store,
        session_id,
        current_user_payload["sub"],
        _is_admin(current_user_payload),
    ):
        raise HTTPException(404, "Session not found")
    return {"status": "ok"}
