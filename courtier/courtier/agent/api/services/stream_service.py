"""Stream service — SSE event generation and state serialization."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time as _time
from pathlib import Path
from typing import Any, AsyncGenerator

from ...artifacts.store import ArtifactStore
from ...core.audit_logger import AuditLogger
from ...core.event_bus import EventBus
from ...telemetry.metrics import set_conversation_tree_branches
from ..sse_adapter import SSEAdapter

logger = logging.getLogger(__name__)


async def _inject_token_counts(
    payload: dict[str, Any],
    session_store: Any,
    session_id: str,
) -> None:
    """Add tokensIn/tokensOut from the persisted session into *payload*."""
    try:
        session = await session_store.get(session_id)
    except Exception:
        logger.exception("Failed to read session for token counts: %s", session_id)
        return
    if session:
        payload["tokensIn"] = session.tokens_in
        payload["tokensOut"] = session.tokens_out


def _sse_json(payload: dict[str, Any]) -> str:
    """Serialize *payload* to a one-line JSON string for SSE."""
    return json.dumps(payload, ensure_ascii=False)


def _event_bus_from_settings(settings: Any) -> EventBus:
    """Create an EventBus using the agent runtime config when available."""
    runtime_cfg = getattr(settings, "agent_runtime", None)
    if runtime_cfg is None:
        return EventBus()
    events_cfg = getattr(runtime_cfg, "events", None)
    if events_cfg is None:
        return EventBus()
    return EventBus(
        default_maxsize=getattr(events_cfg, "default_maxsize", 1000),
        backpressure=getattr(events_cfg, "backpressure", "drop_oldest"),
    )


def _conversation_tree_enabled_from_settings(settings: Any) -> bool:
    """Return whether the conversation tree should be initialised."""
    runtime_cfg = getattr(settings, "agent_runtime", None)
    if runtime_cfg is None:
        return False
    tree_cfg = getattr(runtime_cfg, "conversation_tree", None)
    if tree_cfg is None:
        return False
    return bool(getattr(tree_cfg, "enabled", False))


# -- Serialization helpers for multi-turn state ---------------------------------


def serialize_messages(messages: tuple) -> str:
    """Serialize AgentState messages to a JSON string for persistence."""
    data = []
    for msg in messages:
        d: dict = {"role": msg.role, "content": msg.content}
        if msg.tool_calls:
            d["tool_calls"] = [
                {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                for tc in msg.tool_calls
            ]
        if msg.tool_call_id:
            d["tool_call_id"] = msg.tool_call_id
        if msg.name:
            d["name"] = msg.name
        if msg.source:
            d["source"] = msg.source
        data.append(d)
    return json.dumps(data, ensure_ascii=False)


def deserialize_messages(json_str: str) -> tuple:
    """Deserialize a JSON string back to a tuple of Message objects."""
    from ...core.model import ToolCall
    from ...core.state import Message as _Msg

    raw = json.loads(json_str)
    messages = []
    for d in raw:
        tool_calls = None
        if d.get("tool_calls"):
            tool_calls = []
            for tc in d["tool_calls"]:
                try:
                    tool_calls.append(ToolCall(**tc))
                except (TypeError, ValueError) as exc:
                    logger.warning(
                        "Skipping malformed tool call in deserialized messages: %s", exc
                    )
        messages.append(
            _Msg(
                role=d["role"],
                content=d.get("content"),
                tool_calls=tuple(tool_calls) if tool_calls is not None else None,
                tool_call_id=d.get("tool_call_id"),
                name=d.get("name"),
                source=d.get("source"),
            )
        )
    return tuple(messages)


def reconstruct_state(
    messages_json: str,
    tree_json: str = "",
    current_node_id: str | None = None,
) -> Any:
    """Reconstruct an AgentState from serialised messages and optional tree."""
    from ...core.conversation_tree import ConversationTree
    from ...core.state import AgentState

    if not messages_json:
        return None
    messages = deserialize_messages(messages_json)
    if not messages:
        return None
    tree = None
    if tree_json:
        try:
            tree = ConversationTree.from_serialized(json.loads(tree_json))
        except Exception:
            logger.exception("Failed to deserialize conversation tree")
    return AgentState(
        status="completed",
        messages=messages,
        current_step=0,
        max_steps=20,
        tree=tree,
        current_node_id=current_node_id or (tree.root_id if tree else None),
    )


# Tool name -> primary artifact type mapping for rehydration.
# When a persisted tool result is found in prior messages, we use this
# mapping to register the artifact with the correct type in the new store.
# Audit work now runs as Skills via SkillTool, so persisted results carry
# the skill name (e.g. ``format_audit``) and rehydrate under the default
# type unless listed here.
_TOOL_ARTIFACT_TYPE: dict[str, str] = {
    "parse_document": "docaudit.parsed_document",
}


def rehydrate_artifact_store(
    artifact_store: ArtifactStore,
    messages: tuple,
    cache_dir: str = ".agent_cache",
) -> None:
    """Scan prior messages for __persisted_output__ markers and re-register
    the corresponding artifacts in *artifact_store* by loading the cached
    data from disk.

    This bridges the gap between multi-turn HTTP requests: each request
    creates a fresh ArtifactStore, but the cache files from previous turns
    still exist on disk.  Without rehydration, tools like get_artifact
    fail with "artifact not found" even though the data is available.

    Since ArtifactStore now handles both disk persistence AND ref_map
    management, there is no separate CacheStore to keep in sync —
    ``artifact_store.set_ref()`` is called directly.
    """
    import json as _json

    cache_root = Path(cache_dir).resolve()

    for msg in messages:
        if msg.role != "tool":
            continue
        try:
            content = (
                _json.loads(msg.content)
                if isinstance(msg.content, str)
                else msg.content
            )
        except (_json.JSONDecodeError, TypeError):
            continue
        if not isinstance(content, dict):
            continue
        data = content.get("data")
        if not isinstance(data, dict) or not data.get("__persisted_output__"):
            continue

        ref_id = data.get("ref_id")
        filepath = data.get("file")
        tool_name = msg.name or ""
        if not ref_id or not filepath:
            continue

        # Path-traversal protection: resolve the user-supplied filepath
        # relative to cache_root and verify it stays within bounds.
        # Use Path(filepath) directly (not .name) so stored sub-paths are
        # preserved while still rejecting escapes above cache_root.
        safe_path = (cache_root / Path(filepath)).resolve()
        try:
            safe_path.relative_to(cache_root)
        except ValueError:
            logger.warning(
                "Rejected filepath outside cache_dir: %s (resolved to %s)",
                filepath,
                safe_path,
            )
            continue

        # Determine artifact type from tool name
        artifact_type = _TOOL_ARTIFACT_TYPE.get(tool_name, "core.cached_output")

        # Load the actual data from the cache file
        try:
            artifact_data = _json.loads(safe_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, _json.JSONDecodeError, OSError):
            continue

        artifact_store.register_cached_ref(
            ref_id=ref_id,
            artifact_type=artifact_type,
            created_by=tool_name,
            data=artifact_data,
            role=(
                "primary_document" if tool_name == "parse_document" else "intermediate"
            ),
            subject="current_upload" if tool_name == "parse_document" else "unknown",
            projection_allowed=True,
        )

        # Sync ref_map for multi-turn $ref resolution.
        # ArtifactStore now owns ref_map — no separate CacheStore to sync.
        if ref_id and filepath:
            artifact_store.set_ref(ref_id, str(safe_path))


# -- SSE stream generator helpers ----------------------------------------------


def _prepare_artifact_store_for_session(
    prior_state: Any,
    context_manager: Any,
) -> ArtifactStore:
    """Return the artifact store for the current session.

    Uses ``context_manager._cache`` (the shared ArtifactStore) so that
    sub-agent results persisted by AgentRuntime's summarizer land in the
    same store that the parent agent uses for ``get_artifact`` reads.
    Previously a fresh ``ArtifactStore()`` was created per request, which
    had a different ``ref_map`` and never saw sub-agent-persisted refs.

    For multi-turn conversations, prior-turn persisted outputs are
    rehydrated into this shared store.
    """
    artifact_store = getattr(context_manager, "_cache", None)
    if artifact_store is None:
        artifact_store = ArtifactStore()
    if prior_state is not None and prior_state.messages:
        cache_dir = (
            str(getattr(context_manager, "_cache_dir", ".agent_cache"))
            if context_manager
            else ".agent_cache"
        )
        rehydrate_artifact_store(
            artifact_store,
            prior_state.messages,
            cache_dir=cache_dir,
        )
    return artifact_store


def _build_model_config(settings: Any, model_name: str) -> dict[str, str]:
    """Extract LLM config from settings for the agent run."""
    return {
        "api_key": getattr(settings, "llm_api_key", ""),
        "base_url": getattr(settings, "llm_base_url", ""),
        "model": model_name or getattr(settings, "llm_model", ""),
    }


# -- SSE stream generator ------------------------------------------------------


async def generate_sse_stream(
    *,
    agent: Any,
    session_id: str,
    task: str,
    agent_context: dict,
    session_store: Any,
    settings: Any,
    prior_state: Any = None,
    is_new: bool = True,
    start_step: int = 0,
    context_manager: Any = None,
    model_name: str = "",
    pause_event: Any = None,
    active_tasks: dict | None = None,
    audit_base_dir: str = "",
    audit_log_enabled: bool = True,
    tool_registry: Any = None,
) -> AsyncGenerator[str, None]:
    """Generate SSE events for an agent run.

    All dependencies are passed as keyword arguments so the function is
    testable without a FastAPI request context.
    """
    queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
    event_bus = _event_bus_from_settings(settings)
    adapter = SSEAdapter(
        queue,
        session_store,
        session_id,
        pause_event,
        start_step_index=start_step,
        tool_registry=tool_registry,
    )
    adapter.start_listening(event_bus)

    # Prepare audit logger so sub-agents can write per-run audit logs.
    agent_context["audit_base_dir"] = audit_base_dir
    audit_logger = None
    if audit_log_enabled:
        audit_logger = AuditLogger.for_run(
            agent_name="api_session",
            base_dir=audit_base_dir,
            run_id=session_id,
        )

    # Emit session event as the very first event for new sessions and
    # atomically promote the persisted record from "initial" to "running".
    if is_new:
        session_payload = {
            "type": "session",
            "sessionId": session_id,
            "modelName": model_name,
        }
        yield f"data: {json.dumps(session_payload, ensure_ascii=False)}\n\n"
        await session_store.update(session_id, status="running")
        # Force event-loop scheduling so the chunk is flushed to the
        # client immediately rather than sitting in buffers.
        await asyncio.sleep(0)

    async def runner() -> None:
        try:
            artifact_store = _prepare_artifact_store_for_session(
                prior_state, context_manager
            )
            model_config = _build_model_config(settings, model_name)
            result = await agent.run(
                task=task,
                context=agent_context,
                event_bus=event_bus,
                session_id=session_id,
                on_subagent_event=adapter.on_subagent_event,
                model_config=model_config,
                context_manager=context_manager,
                state=prior_state,
                audit_logger=audit_logger,
                artifact_store=artifact_store,
                use_tree=_conversation_tree_enabled_from_settings(settings),
            )
            flushed = adapter.flush_verdict()
            conclusion = flushed or (result.content or "")
            # Persist final messages and conversation tree for future
            # multi-turn continuation and branching.
            if result.final_state is not None:
                update_kwargs: dict[str, Any] = {
                    "messages_json": serialize_messages(result.final_state.messages),
                }
                if result.final_state.tree is not None:
                    update_kwargs["tree_json"] = json.dumps(
                        result.final_state.tree.serialize(), ensure_ascii=False
                    )
                    update_kwargs["current_node_id"] = result.final_state.current_node_id
                    set_conversation_tree_branches(
                        session_id, len(result.final_state.tree.leaf_nodes())
                    )
                await session_store.update(session_id, **update_kwargs)
            if result.final_state is not None and result.final_state.status == "error":
                # Model/internal failure: surface an explicit error event
                # instead of a silent "complete" so the frontend can show
                # the failure state. The detailed reason stays server-side.
                logger.warning(
                    "Agent run ended in error state for session %s: %s",
                    session_id,
                    result.final_state.termination_reason,
                )
                await queue.put(
                    (
                        "error",
                        {
                            "type": "error",
                            "detail": "模型调用失败，请稍后重试",
                        },
                    )
                )
            else:
                await queue.put(("complete", conclusion))
        except asyncio.CancelledError:
            logger.info("Agent run cancelled for session %s", session_id)
            await session_store.update(
                session_id, status="stopped", finished_at=_time.time()
            )
            await queue.put(("stopped", None))
        except Exception:
            trace_id = secrets.token_hex(8)
            logger.exception(
                "Agent run failed for session %s (trace_id=%s)",
                session_id,
                trace_id,
            )
            await queue.put(
                (
                    "error",
                    {
                        "type": "error",
                        "detail": "服务器内部错误，请稍后重试",
                        "trace_id": trace_id,
                    },
                )
            )
        finally:
            adapter.stop_listening()
            # Close the model client to prevent AsyncHttpxClientWrapper.__del__
            # from scheduling a bare aclose() task that crashes with
            # "AttributeError: ... object has no attribute '_transport'".
            try:
                await agent.model.close()
            except Exception:
                logger.debug(
                    "Error closing model client for session %s",
                    session_id,
                    exc_info=True,
                )
            await queue.put(("done", None))

    task_ref = asyncio.create_task(runner())
    if active_tasks is not None:
        active_tasks[session_id] = task_ref

    try:
        while True:
            item = await queue.get()
            tag = item[0]
            if tag == "done":
                break
            if tag == "event":
                yield item[1]
            elif tag == "complete":
                now = _time.time()
                conclusion = item[1] or ""
                await session_store.update(
                    session_id,
                    status="completed",
                    finished_at=now,
                    conclusion=conclusion,
                )
                await session_store.finalize_turn_conclusion(session_id, conclusion)
                payload = {"type": "complete"}
                if conclusion:
                    payload["conclusion"] = conclusion
                await _inject_token_counts(payload, session_store, session_id)
                yield f"data: {_sse_json(payload)}\n\n"
            elif tag == "stopped":
                payload = {"type": "stopped"}
                await _inject_token_counts(payload, session_store, session_id)
                yield f"data: {_sse_json(payload)}\n\n"
            elif tag == "error":
                await session_store.update(
                    session_id,
                    status="error",
                    finished_at=_time.time(),
                    error_detail=item[1].get("detail", ""),
                )
                payload = dict(item[1])
                await _inject_token_counts(payload, session_store, session_id)
                yield f"data: {_sse_json(payload)}\n\n"
    finally:
        # Cancel the runner when the SSE consumer disconnects so the agent
        # loop does not keep running and filling the queue indefinitely.
        task_ref.cancel()
        try:
            await asyncio.wait_for(task_ref, timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        except Exception:
            logger.exception(
                "Unhandled error while cancelling runner for session %s",
                session_id,
            )
        if active_tasks is not None:
            active_tasks.pop(session_id, None)
