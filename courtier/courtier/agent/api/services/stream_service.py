"""Stream service — SSE event generation and state serialization."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time as _time
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
                {"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in msg.tool_calls
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
                    logger.warning("Skipping malformed tool call in deserialized messages: %s", exc)
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


# -- SSE stream generator helpers ----------------------------------------------


def _prepare_artifact_store_for_session(
    prior_state: Any,
    context_manager: Any,
    artifact_snapshot: str = "",
) -> ArtifactStore:
    """Return the artifact store for the current session.

    Uses ``context_manager._cache`` — a fresh store created per request by
    the agent builders (sessions no longer share ``app.state.artifact_store``,
    which leaked artifacts — and thus prior conversations' document content —
    across sessions).  Within the run, sub-agent results persisted by
    AgentRuntime's summarizer land in the same store that the parent agent
    uses for ``get_artifact`` reads.

    Multi-turn restore goes exclusively through the persisted artifact
    snapshot (full fidelity — typed artifacts, type policies, ref_map).
    Sessions saved before snapshots existed are not restorable.
    """
    artifact_store = getattr(context_manager, "_cache", None)
    if artifact_store is None:
        artifact_store = ArtifactStore()

    if artifact_snapshot:
        try:
            artifact_store.load_snapshot(json.loads(artifact_snapshot))
        except Exception:
            logger.warning(
                "Failed to restore artifact snapshot for session",
                exc_info=True,
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
    artifact_snapshot: str = "",
    context_state: str = "",
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
        # Prefer the agent's own registry — it contains plugin tools AND
        # builtin artifact tools AND SkillTools, so tool metadata
        # (display_name) resolves for all of them. The app-level registry
        # only holds plugin tools.
        tool_registry=getattr(agent, "tool_registry", None) or tool_registry,
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
            # Restore compaction state so the re-compaction guard and the
            # compact numbering survive across requests.
            if context_state and context_manager is not None:
                try:
                    context_manager.load_state(json.loads(context_state))
                except Exception:
                    logger.warning(
                        "Failed to restore compact state for session %s",
                        session_id,
                        exc_info=True,
                    )
            artifact_store = _prepare_artifact_store_for_session(
                prior_state, context_manager, artifact_snapshot
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
            # Dual-format convergence point: ``messages_json`` is the primary
            # format; ``tree_json`` is a parallel format for the
            # conversation-tree migration (see
            # courtier/docs/architecture/pi-architecture-migration-plan.md).
            # Retirement condition for tree_json: all tree read paths are
            # fully covered AND pre-migration sessions have expired.
            if result.final_state is not None:
                update_kwargs: dict[str, Any] = {
                    "messages_json": serialize_messages(result.final_state.messages),
                }
                # Persist the full artifact store state so the next turn
                # restores typed artifacts/policies exactly, not just refs.
                # Best-effort: a snapshot failure must not fail the run.
                try:
                    update_kwargs["artifact_snapshot"] = json.dumps(
                        artifact_store.snapshot(), ensure_ascii=False
                    )
                    if context_manager is not None:
                        update_kwargs["context_state"] = json.dumps(
                            context_manager.snapshot_state(), ensure_ascii=False
                        )
                except Exception:
                    logger.warning(
                        "Failed to snapshot artifact store for session %s",
                        session_id,
                        exc_info=True,
                    )
                if result.final_state.tree is not None:
                    update_kwargs["tree_json"] = json.dumps(
                        result.final_state.tree.serialize(), ensure_ascii=False
                    )
                    update_kwargs["current_node_id"] = result.final_state.current_node_id
                    set_conversation_tree_branches(
                        session_id, len(result.final_state.tree.leaf_nodes())
                    )
                # Persist the domain activation set so per-request rebuilds
                # can replay it (context compaction drops the evidence).
                activator = getattr(agent, "_domain_activator", None)
                if activator is not None:
                    update_kwargs["active_domains"] = sorted(activator.active_domains)
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
            await session_store.update(session_id, status="stopped", finished_at=_time.time())
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
