"""Session routes — list, get, delete, branch, and SSE streaming."""

from __future__ import annotations

import logging
import secrets
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..middleware.auth import _is_admin, get_current_user
from ..rate_limiter import limiter
from ..services.agent_service import build_audit_agent, build_chat_agent
from ..services.session_service import (
    delete_session,
    fork_session_tree,
    get_session,
    list_sessions,
    rewind_session_tree,
)
from ..services.stream_service import generate_sse_stream, reconstruct_state

logger = logging.getLogger(__name__)

router = APIRouter()


async def _resolve_audit_file_or_404(
    file_store: Any,
    file_id: str,
    upload_dir: str,
) -> Path:
    """Resolve a file_id to an existing path or raise HTTPException(404)."""
    file_path = cast(Path | None, await file_store.resolve_path(file_id, upload_dir))
    if file_path is None or not file_path.exists():
        raise HTTPException(404, f"文件不存在: {file_id}")
    return file_path


async def _resolve_audit_file_owned_or_404(
    file_store: Any,
    file_id: str,
    upload_dir: str,
    current_user: str,
    is_admin: bool,
) -> Path:
    """Resolve a file_id and verify ownership before returning its path."""
    file_info = cast(Any | None, await file_store.resolve(file_id))
    if file_info is None:
        raise HTTPException(404, f"文件不存在: {file_id}")
    if not is_admin and file_info.owner and file_info.owner != current_user:
        raise HTTPException(403, "无权访问该文件")
    file_path = cast(Path | None, await file_store.resolve_path(file_id, upload_dir))
    if file_path is None or not file_path.exists():
        raise HTTPException(404, f"文件不存在: {file_id}")
    return file_path


def _resolve_skills_dir(courtier_config: Any) -> str:
    """Return the skills directory of the first configured domain package.

    Raises HTTPException(500) if no domain packages are configured.
    """
    domains = getattr(courtier_config, "domains", None) or []
    if not domains:
        raise HTTPException(500, "未配置 domain package")
    return str(domains[0].skills_path)


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
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=1000),
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
        return await list_sessions(
            session_store, current_user, is_admin, skip=skip, limit=limit
        )

    # Both modes need at least a task
    if not task:
        raise HTTPException(400, "task 参数必须提供")

    pause_event = getattr(request.app.state, "pause_event", None)
    active_tasks = getattr(request.app.state, "active_tasks", None)
    tool_registry = getattr(request.app.state, "tool_registry", None)

    # Determine mode: new session vs continue existing
    if sessionId:
        # Continue existing session
        existing = await session_store.get_owned(
            sessionId, current_user, is_admin
        )
        if existing is None:
            raise HTTPException(404, "Session not found")

        session_id = sessionId
        prior_state = reconstruct_state(
            existing.messages_json,
            existing.tree_json,
            existing.current_node_id,
        )
        start_step = len(existing.steps)

        # Record the new turn boundary so historical sessions render
        # each turn with the correct user message and step grouping.
        await session_store.add_turn(sessionId, task)

        if existing.file_id:
            # Audit mode continuation
            file_store = request.app.state.file_store
            file_path = await _resolve_audit_file_owned_or_404(
                file_store,
                existing.file_id,
                settings.upload_dir,
                current_user,
                is_admin,
            )
            agent, context_manager, model_name = await _build_agent_or_500(
                lambda: build_audit_agent(
                    settings,
                    plugin_system=request.app.state.plugin_system,
                    tool_registry=request.app.state.tool_registry,
                    cache_store=request.app.state.cache_store,
                    skills_dir=_resolve_skills_dir(request.app.state.courtier_config),
                    prompt_engine=request.app.state.prompt_engine,
                ),
                session_id,
                "audit",
            )
            agent_context = {"file_path": str(file_path)}
        else:
            # Chat mode continuation
            agent, context_manager, model_name = await _build_agent_or_500(
                lambda: build_chat_agent(
                    settings,
                    cache_store=request.app.state.cache_store,
                    prompt_engine=request.app.state.prompt_engine,
                ),
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
            file_path = await _resolve_audit_file_owned_or_404(
                file_store,
                fileId,
                settings.upload_dir,
                current_user,
                is_admin,
            )
            file_info = await file_store.resolve(fileId)
            file_name = (
                file_info.original_name if file_info else ""
            ) or ""

            agent, context_manager, model_name = await _build_agent_or_500(
                lambda: build_audit_agent(
                    settings,
                    plugin_system=request.app.state.plugin_system,
                    tool_registry=request.app.state.tool_registry,
                    cache_store=request.app.state.cache_store,
                    skills_dir=_resolve_skills_dir(request.app.state.courtier_config),
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
                lambda: build_chat_agent(
                    settings,
                    cache_store=request.app.state.cache_store,
                    prompt_engine=request.app.state.prompt_engine,
                ),
                session_id,
                "chat",
            )
            agent_context = {}

        is_new = True

    # Create or update session record. New sessions start as "initial" and are
    # promoted to "running" only when the SSE stream actually begins emitting
    # events. This avoids leaving orphan "running" records if the runner fails
    # before the first chunk is yielded.
    if is_new:
        await session_store.create(
            session_id=session_id,
            task=task,
            file_id=fileId or "",
            file_name=file_name,
            model_name=model_name,
            owner=current_user,
            status="initial",
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
@limiter.limit("30/minute")
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
@limiter.limit("10/minute")
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


class ForkRequest(BaseModel):
    """Request body for forking a conversation tree node."""

    node_id: Optional[str] = None
    reason: str = ""


class RewindRequest(BaseModel):
    """Request body for rewinding to a conversation tree node."""

    node_id: str


@router.post("/sessions/{session_id}/fork")
@limiter.limit("30/minute")
async def fork_session(
    session_id: str,
    request: ForkRequest,
    request_obj: Request,
    current_user_payload: dict = Depends(get_current_user),
):
    """Fork the current conversation tree node and return the new branch."""
    return await fork_session_tree(
        request_obj.app.state.session_store,
        current_user_payload["sub"],
        _is_admin(current_user_payload),
        session_id,
        request.node_id,
        request.reason,
    )


@router.post("/sessions/{session_id}/rewind")
@limiter.limit("30/minute")
async def rewind_session(
    session_id: str,
    request: RewindRequest,
    request_obj: Request,
    current_user_payload: dict = Depends(get_current_user),
):
    """Rewind the conversation tree to an existing node."""
    return await rewind_session_tree(
        request_obj.app.state.session_store,
        current_user_payload["sub"],
        _is_admin(current_user_payload),
        session_id,
        request.node_id,
    )
