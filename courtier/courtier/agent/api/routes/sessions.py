"""Session routes — list, get, delete, branch, and SSE streaming."""

from __future__ import annotations

import json
import logging
import secrets
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..middleware.auth import _is_admin, get_current_user
from ..rate_limiter import limiter
from ..services.agent_service import build_agent
from ..services.run_manager import RunManager, generate_sse_stream, stream_run
from ..services.session_service import (
    delete_session,
    fork_session_tree,
    get_session,
    list_sessions,
    rewind_session_tree,
    truncate_session_to_turn,
)
from ..services.stream_service import reconstruct_state

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


async def _persist_oversized_task(task: str, context_manager: Any, settings: Any) -> str:
    """Persist oversized user input to disk, replacing it with a ref marker.

    Applies the Layer-1 mechanism to user pastes: the model sees a preview
    plus a ``$ref`` it can expand via ``get_artifact`` instead of the full
    text occupying the context window.  The original task is still what the
    session record/turn history stores (captured before this call), so the
    user-facing display is unaffected.
    """
    limit = int(getattr(settings, "context_max_user_message_chars", 10000))
    if len(task) <= limit:
        return task
    store = getattr(context_manager, "_cache", None)
    if store is None:
        return task
    try:
        result = await store.persist(task, "user_input", force=True)
    except Exception:
        logger.warning("Failed to persist oversized user input", exc_info=True)
        return task
    if not result.persisted:
        return task
    preview = result.data.get("preview", "") if isinstance(result.data, dict) else ""
    return (
        f"用户输入较长（共 {len(task)} 字符），完整内容已保存为 {result.ref_id}，"
        f"需要时请通过 get_artifact 获取。\n"
        f"内容预览：\n{preview}"
    )


@router.get("/sessions")
@limiter.limit("10/minute")
async def handle_sessions(
    request: Request,
    task: Optional[str] = Query(default=None),
    fileId: Optional[str] = Query(default=None),
    sessionId: Optional[str] = Query(default=None),
    editTurn: Optional[int] = Query(default=None),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=1000),
    current_user_payload: dict = Depends(get_current_user),
):
    """List all sessions, create a new one, or continue an existing one.

    - No params: list all historical sessions.
    - task (+ optional fileId), no sessionId: new session (emits session SSE event).
    - task + sessionId: continue existing multi-turn session.
    - task + sessionId + editTurn: edit-resend — revoke turn `editTurn` and
      everything after it, then re-run the turn with the edited `task`.
    """
    settings = request.app.state.settings
    session_store = request.app.state.session_store
    current_user = current_user_payload["sub"]
    is_admin = _is_admin(current_user_payload)

    # List mode: no query params
    if task is None and fileId is None and sessionId is None:
        items = await list_sessions(session_store, current_user, is_admin, skip=skip, limit=limit)
        run_manager = getattr(request.app.state, "run_manager", None)
        if run_manager is not None:
            # Overlay live run status — store writes can lag the runner.
            for item in items:
                live = run_manager.active_status(item["id"])
                if live is not None:
                    item["status"] = live
        return items

    # Both modes need at least a task
    if not task:
        raise HTTPException(400, "task 参数必须提供")

    # Edit-resend only makes sense against an existing session.
    if editTurn is not None and not sessionId:
        raise HTTPException(400, "editTurn 仅用于续轮会话")

    run_manager = getattr(request.app.state, "run_manager", None)

    # Determine mode: new session vs continue existing
    if sessionId:
        # Continue existing session
        existing = await session_store.get_owned(sessionId, current_user, is_admin)
        if existing is None:
            raise HTTPException(404, "Session not found")

        # One run per session: a currently-executing run conflicts with a
        # new turn / edit-resend. A terminal run kept for its grace period
        # does NOT — the next turn may start while late observers replay.
        if run_manager is not None and run_manager.has_running(sessionId):
            if editTurn is not None:
                raise HTTPException(409, "会话正在运行，请先停止再编辑")
            # Native EventSource reconnects replay the exact same URL — a
            # browser re-attach, not a new turn. Serve it from the live run's
            # transcript instead of registering a duplicate turn (the old
            # implementation re-ran the turn on every reconnect).
            if task == existing.task and task == existing.turn_messages[-1]["text"]:
                return await _attach_stream_or_404(
                    request, run_manager, sessionId, existing.event_seq
                )
            raise HTTPException(409, "会话正在运行，请先停止或等待其完成")

        # Edit-resend: revoke turn `editTurn` (and everything after it)
        # before the continuation registers the edited text as a fresh turn.
        edited_turn_file_name: str | None = None
        if editTurn is not None:
            if 0 <= editTurn < len(existing.turn_messages):
                # The edited turn keeps its original upload (edit = text only).
                edited_turn_file_name = existing.turn_messages[editTurn].get("fileName")
            existing = await truncate_session_to_turn(
                session_store, current_user, is_admin, sessionId, editTurn
            )
            if editTurn == 0:
                # The conversation title is the first turn's text.
                await session_store.update(sessionId, task=task[:200])

        session_id = sessionId
        prior_state = reconstruct_state(
            existing.messages_json,
            existing.tree_json,
            existing.current_node_id,
        )
        artifact_snapshot = existing.artifact_snapshot
        context_state = existing.context_state
        start_step = len(existing.steps)
        # Activation state is persisted per session and replayed on rebuild —
        # it must not be derived from history (compaction drops the evidence).
        active_domains = tuple(existing.active_domains or [])

        # A file uploaded in THIS turn supersedes the session's original
        # file; without it the continuation would silently ignore the new
        # upload (chat continuation has no file_path at all).  For
        # edit-resend the client re-sends the edited turn's original fileId,
        # which also rolls a later turn's supersession back to that file.
        effective_file_id = fileId or existing.file_id
        turn_file_name = edited_turn_file_name
        if fileId:
            file_store = request.app.state.file_store
            file_info = await file_store.resolve(fileId)
            if fileId != existing.file_id:
                await session_store.update(
                    sessionId,
                    file_id=fileId,
                    file_name=(file_info.original_name if file_info else ""),
                )
            if turn_file_name is None:
                turn_file_name = file_info.original_name if file_info else None

        # Record the new turn boundary so historical sessions render
        # each turn with the correct user message and step grouping.  The
        # turn entry carries the upload reference so restored sessions keep
        # the file chip (and can re-send it on a later edit).
        await session_store.add_turn(sessionId, task, file_name=turn_file_name, file_id=fileId)

        is_new = False
    else:
        # New session
        session_id = f"sess_{secrets.token_hex(6)}"
        prior_state = None
        artifact_snapshot = ""
        context_state = ""
        start_step = 0
        effective_file_id = fileId
        active_domains = ()
        is_new = True

    # One unified builder for every session shape: chat-only, uploaded
    # document, and multi-turn continuation.
    file_name = ""
    agent_context: dict[str, str] = {}
    if effective_file_id:
        file_store = request.app.state.file_store
        file_path = await _resolve_audit_file_owned_or_404(
            file_store,
            effective_file_id,
            settings.upload_dir,
            current_user,
            is_admin,
        )
        file_info = await file_store.resolve(effective_file_id)
        file_name = (file_info.original_name if file_info else "") or ""
        agent_context = {"file_path": str(file_path)}

    agent, context_manager, model_name = await _build_agent_or_500(
        lambda: build_agent(
            settings,
            plugin_system=request.app.state.plugin_system,
            tool_registry=request.app.state.tool_registry,
            courtier_config=request.app.state.courtier_config,
            prompt_engine=request.app.state.prompt_engine,
            owner_id=current_user_payload.get("uid"),
            session_id=session_id,
            shared_plugin_names=request.app.state.shared_plugin_names,
            active_domains=active_domains,
        ),
        session_id,
        "orchestrator",
    )

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

    # Oversized user pastes go to disk (Layer-1 mechanism) — the model sees a
    # preview + $ref instead of the full text.  Done after the session/turn
    # records above so the user-facing display keeps the original input.
    task = await _persist_oversized_task(task, context_manager, settings)

    # The run's log continues the session's event_seq watermark so attach
    # replays stay aligned across turns (new sessions: fresh record → 0+1).
    initial_seq = existing.event_seq + 1 if not is_new else 1

    return StreamingResponse(
        generate_sse_stream(
            agent=agent,
            session_id=session_id,
            task=task,
            agent_context=agent_context,
            session_store=session_store,
            settings=settings,
            run_manager=run_manager,
            user=current_user,
            prior_state=prior_state,
            artifact_snapshot=artifact_snapshot,
            context_state=context_state,
            is_new=is_new,
            start_step=start_step,
            context_manager=context_manager,
            model_name=model_name,
            initial_seq=initial_seq,
        ),
        media_type="text/event-stream",
        headers=_sse_headers(),
    )


def _sse_headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }


async def _attach_stream_or_404(
    request: Request,
    run_manager: RunManager | None,
    session_id: str,
    fallback_since: int,
) -> StreamingResponse:
    """Attach to a session's live run as a streaming observer.

    ``Last-Event-ID`` (sent automatically by EventSource on reconnect) takes
    precedence over the fallback watermark; neither applies when the run is
    gone — the caller 404s and the client falls back to the snapshot path.
    """
    if run_manager is None:
        raise HTTPException(404, "会话没有可接续的运行")
    since: int | None = None
    last_event_id = request.headers.get("last-event-id")
    if last_event_id is not None and last_event_id.strip().isdigit():
        since = int(last_event_id.strip())
    else:
        since = fallback_since
    attached = run_manager.attach(session_id, since)
    if attached is None:
        raise HTTPException(404, "会话没有可接续的运行")
    run, reader = attached
    return StreamingResponse(
        stream_run(run, reader),
        media_type="text/event-stream",
        headers=_sse_headers(),
    )


@router.get("/sessions/{session_id}/events")
@limiter.limit("30/minute")
async def attach_session_events(
    session_id: str,
    request: Request,
    since: Optional[int] = Query(default=None),
    current_user_payload: dict = Depends(get_current_user),
):
    """Attach to the session's active run: replay events after *since*, then
    stream live until the run terminates. 404 when the run is gone — the
    client falls back to the persisted snapshot."""
    session_store = request.app.state.session_store
    existing = await session_store.get_owned(
        session_id, current_user_payload["sub"], _is_admin(current_user_payload)
    )
    if existing is None:
        raise HTTPException(404, "Session not found")
    run_manager = getattr(request.app.state, "run_manager", None)
    return await _attach_stream_or_404(
        request, run_manager, session_id, since if since is not None else existing.event_seq
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
    """Delete a historical session (and GC its unshared cache files)."""
    session_store = request.app.state.session_store
    settings = request.app.state.settings
    if not await delete_session(
        session_store,
        session_id,
        current_user_payload["sub"],
        _is_admin(current_user_payload),
        cache_dir=str(getattr(settings, "cache_dir", "") or ""),
    ):
        raise HTTPException(404, "Session not found")
    return {"status": "ok"}


class SessionPatchRequest(BaseModel):
    """Request body for renaming / pinning a session."""

    task: Optional[str] = None
    pinned: Optional[bool] = None


@router.patch("/sessions/{session_id}")
@limiter.limit("30/minute")
async def patch_session_handler(
    session_id: str,
    patch: SessionPatchRequest,
    request: Request,
    current_user_payload: dict = Depends(get_current_user),
):
    """Rename a session (task) or pin/unpin it in the history list."""
    session_store = request.app.state.session_store
    existing = await session_store.get_owned(
        session_id, current_user_payload["sub"], _is_admin(current_user_payload)
    )
    if existing is None:
        raise HTTPException(404, "Session not found")

    updates: dict[str, Any] = {}
    if patch.task is not None:
        task = patch.task.strip()
        if not task:
            raise HTTPException(400, "标题不能为空")
        updates["task"] = task[:200]
    if patch.pinned is not None:
        updates["pinned"] = bool(patch.pinned)
    if not updates:
        raise HTTPException(400, "没有需要更新的字段")

    updated = await session_store.update(session_id, **updates)
    if updated is None:
        raise HTTPException(404, "Session not found")
    return updated.to_summary_dict()


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


@router.post("/sessions/{session_id}/compact")
@limiter.limit("10/minute")
async def compact_session(
    session_id: str, request: Request, current_user_payload: dict = Depends(get_current_user)
):
    """Force a full context compaction of the session history (manual /compact).

    Unlike budget-triggered compaction this compresses everything including
    the current turn, then persists the compacted messages and the compact
    state back to the session record.
    """
    from ...core.context_manager import ContextManager
    from ..services.agent_service import (
        _compact_prompt_kwargs,
        _context_budget_kwargs,
        build_model_client,
    )
    from ..services.stream_service import deserialize_messages, serialize_messages

    session_store = request.app.state.session_store
    settings = request.app.state.settings
    existing = await session_store.get_owned(
        session_id, current_user_payload["sub"], _is_admin(current_user_payload)
    )
    if existing is None:
        raise HTTPException(404, "Session not found")
    if not existing.messages_json:
        raise HTTPException(400, "会话还没有可压缩的上下文")

    messages = deserialize_messages(existing.messages_json)
    cm = ContextManager(
        model=build_model_client(settings),
        cache_dir=settings.cache_dir,
        **_context_budget_kwargs(settings),
        **_compact_prompt_kwargs(getattr(request.app.state, "prompt_engine", None)),
    )
    before_tokens = cm.estimate_tokens(messages)
    compacted = await cm.force_compact(messages)
    after_tokens = cm.estimate_tokens(compacted)

    await session_store.update(
        session_id,
        messages_json=serialize_messages(compacted),
        context_state=json.dumps(cm.snapshot_state(), ensure_ascii=False),
    )
    return {
        "beforeTokens": before_tokens,
        "afterTokens": after_tokens,
        "beforeMessages": len(messages),
        "afterMessages": len(compacted),
        "compactCount": cm.state.compact_count,
    }
