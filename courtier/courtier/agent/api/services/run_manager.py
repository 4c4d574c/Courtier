"""RunManager — decouples agent runs from SSE connections.

A run (one session turn) is an independent background task. SSE connections
are pure observers reading the run's ``RunEventLog`` (replay + live tail):
they may attach, detach and re-attach freely without affecting the run.
Terminal persistence (status/conclusion/messages_json) happens entirely on
the run side, whether or not anyone is watching.

Lifecycle:
    running → completed | error | stopped   (terminal; kept for a grace
                                              period so late attaches can
                                              still replay the full log)

On restart every in-process run is gone: the lifespan startup sweep marks
persisted ``running`` sessions as ``interrupted`` (see sweep_stale_sessions).
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time as _time
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator

from ...core.audit_logger import AuditLogger
from ...core.event_bus import EventBus
from ...telemetry.metrics import set_conversation_tree_branches
from ..session_store import SessionStore
from ..sse_adapter import RunRecorder
from .run_event_log import RunEventLog, RunEventReader

logger = logging.getLogger(__name__)


class RunConflictError(RuntimeError):
    """Raised when a session already has an active (non-terminal) run."""


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


def _build_model_config(settings: Any, model_name: str) -> dict[str, str]:
    """Extract LLM config from settings for the agent run."""
    return {
        "api_key": getattr(settings, "llm_api_key", ""),
        "base_url": getattr(settings, "llm_base_url", ""),
        "model": model_name or getattr(settings, "llm_model", ""),
    }


def _prepare_artifact_store_for_session(
    prior_state: Any,
    context_manager: Any,
    artifact_snapshot: str = "",
    session_id: str = "",
) -> Any:
    """Return the artifact store for the current session run.

    Multi-turn restore goes exclusively through the persisted artifact
    snapshot (full fidelity — typed artifacts, type policies, ref_map).
    """
    from ...artifacts.store import ArtifactStore

    artifact_store = getattr(context_manager, "_cache", None)
    if artifact_store is None:
        artifact_store = ArtifactStore(session_id=session_id)

    if artifact_snapshot:
        try:
            artifact_store.load_snapshot(json.loads(artifact_snapshot))
        except Exception:
            logger.warning(
                "Failed to restore artifact snapshot for session",
                exc_info=True,
            )
    return artifact_store


@dataclass
class RunSpec:
    """Everything one run needs; produced by the session route.

    ``initial_seq`` is ``record.event_seq + 1`` so the run's log seqs stay
    monotonic per session across turns (attach replays align with the
    persisted watermark).
    """

    session_id: str
    user: str
    task: str
    agent: Any  # prebuilt by the route (queued runs defer build in Task 4.1)
    context_manager: Any = None
    model_name: str = ""
    agent_context: dict[str, Any] = field(default_factory=dict)
    prior_state: Any = None
    artifact_snapshot: str = ""
    context_state: str = ""
    is_new: bool = True
    start_step: int = 0
    initial_seq: int = 0


class AgentRun:
    """One running (or recently finished) session turn."""

    def __init__(self, session_id: str, user: str, log: RunEventLog, bus: EventBus) -> None:
        self.session_id = session_id
        self.user = user
        self.status = "running"  # running | completed | error | stopped
        self.log = log
        self.bus = bus
        self.recorder: RunRecorder | None = None
        self.task: asyncio.Task | None = None
        self.created_at = _time.time()
        self.finished_at: float | None = None

    @property
    def terminal(self) -> bool:
        return self.status != "running"


class RunManager:
    """Registry of active runs; owns run lifecycle and admission."""

    def __init__(
        self,
        *,
        session_store: SessionStore,
        settings: Any,
        pause_event: asyncio.Event | None = None,
    ) -> None:
        self._store = session_store
        self._settings = settings
        self._pause_event = pause_event
        self._runs: dict[str, AgentRun] = {}
        self._lock = asyncio.Lock()
        self._grace_seconds = getattr(settings, "run_grace_seconds", 600)
        self._log_max_events = getattr(settings, "run_log_max_events", 50_000)
        self._log_max_bytes = getattr(settings, "run_log_max_bytes", 8 * 1024 * 1024)

    # -- Registry ------------------------------------------------------------

    def _active_run(self, session_id: str) -> AgentRun | None:
        """Active or in-grace run for *session_id*; lazily evicts expired ones."""
        run = self._runs.get(session_id)
        if run is None:
            return None
        if (
            run.terminal
            and run.finished_at is not None
            and _time.time() - run.finished_at > self._grace_seconds
        ):
            self._runs.pop(session_id, None)
            return None
        return run

    def has_active(self, session_id: str) -> bool:
        return self._active_run(session_id) is not None

    def has_running(self, session_id: str) -> bool:
        """True only while the session's run is actually executing.

        Routes conflict-check with this: a terminal run kept for its grace
        period must not block the next turn (late observers can still attach
        to replay it — see ``attach``).
        """
        run = self._active_run(session_id)
        return run is not None and not run.terminal

    def attach(
        self, session_id: str, since: int | None = None
    ) -> tuple[AgentRun, RunEventReader] | None:
        """Attach an observer to a live (or in-grace) run; None if absent."""
        run = self._active_run(session_id)
        if run is None:
            return None
        return run, run.log.reader(since)

    def active_status(self, session_id: str) -> str | None:
        """Live status overlay for list responses (store writes can lag)."""
        run = self._active_run(session_id)
        return run.status if run is not None else None

    def active_session_ids(self) -> list[str]:
        return [sid for sid in list(self._runs) if self._active_run(sid) is not None]

    # -- Lifecycle -------------------------------------------------------------

    async def start(self, spec: RunSpec) -> AgentRun:
        """Start a run; raises RunConflictError if the session already has one.

        A *terminal* run kept for its grace period does not conflict: the
        next turn may start while late observers still replay the previous
        run's transcript.
        """
        async with self._lock:
            existing = self._active_run(spec.session_id)
            if existing is not None and not existing.terminal:
                raise RunConflictError(
                    f"session {spec.session_id} already has an active run ({existing.status})"
                )
            log = RunEventLog(
                max_events=self._log_max_events,
                max_bytes=self._log_max_bytes,
                initial_seq=spec.initial_seq,
            )
            bus = _event_bus_from_settings(self._settings)
            run = AgentRun(spec.session_id, spec.user, log, bus)
            self._runs[spec.session_id] = run
        run.task = asyncio.create_task(self._runner(run, spec), name=f"run:{spec.session_id}")
        return run

    async def stop(self, session_id: str) -> bool:
        """Cancel a running run (or dequeue a queued one in Task 4.1)."""
        async with self._lock:
            run = self._active_run(session_id)
        if run is None or run.terminal or run.task is None:
            return False
        run.task.cancel()
        try:
            await asyncio.wait_for(run.task, timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            logger.warning("Timeout awaiting run cancellation for %s", session_id)
        except Exception:
            logger.exception("Error awaiting run cancellation for %s", session_id)
        return True

    async def stop_all(self) -> list[str]:
        stopped: list[str] = []
        for sid in self.active_session_ids():
            run = self._runs.get(sid)
            if run is not None and not run.terminal:
                if await self.stop(sid):
                    stopped.append(sid)
        return stopped

    async def shutdown(self) -> None:
        """App shutdown: cancel everything still running."""
        await self.stop_all()

    async def sweep_stale_sessions(self) -> int:
        """Startup sweep: persisted running/queued sessions died with the old
        process — mark them interrupted. Returns the number swept."""
        summaries = await self._store.list_all(current_user="", is_admin=True, limit=-1)
        swept = 0
        for item in summaries:
            if item.get("status") in ("running", "queued"):
                await self._store.update(item["id"], status="interrupted", finished_at=_time.time())
                swept += 1
        if swept:
            logger.info("Startup sweep marked %d stale session(s) interrupted", swept)
        return swept

    # -- Runner ------------------------------------------------------------------

    async def _runner(self, run: AgentRun, spec: RunSpec) -> None:
        """The run body — migrated from the old SSE-generator runner.

        All terminal persistence happens here regardless of observers; the
        runner never touches a connection-owned queue.
        """
        from .stream_service import serialize_messages

        session_id = spec.session_id
        store = self._store
        try:
            if spec.is_new:
                run.log.append(
                    {
                        "type": "session",
                        "sessionId": session_id,
                        "modelName": spec.model_name,
                    }
                )
            await store.update(session_id, status="running", error_detail=None)

            recorder = RunRecorder(
                run.log,
                store,
                session_id,
                self._pause_event,
                start_step_index=spec.start_step,
                tool_registry=getattr(spec.agent, "tool_registry", None),
            )
            run.recorder = recorder
            recorder.start_listening(run.bus)

            audit_logger = None
            if getattr(self._settings, "audit_log_enabled", True):
                audit_logger = AuditLogger.for_run(
                    agent_name="api_session",
                    base_dir=getattr(self._settings, "audit_log_dir", ""),
                    run_id=session_id,
                )
            audit_dir = getattr(self._settings, "audit_log_dir", "")
            spec.agent_context.setdefault("audit_base_dir", audit_dir)

            if spec.context_state and spec.context_manager is not None:
                try:
                    spec.context_manager.load_state(json.loads(spec.context_state))
                except Exception:
                    logger.warning(
                        "Failed to restore compact state for session %s",
                        session_id,
                        exc_info=True,
                    )
            artifact_store = _prepare_artifact_store_for_session(
                spec.prior_state,
                spec.context_manager,
                spec.artifact_snapshot,
                session_id=session_id,
            )
            result = await spec.agent.run(
                task=spec.task,
                context=spec.agent_context,
                event_bus=run.bus,
                session_id=session_id,
                on_subagent_event=recorder.on_subagent_event,
                model_config=_build_model_config(self._settings, spec.model_name),
                context_manager=spec.context_manager,
                state=spec.prior_state,
                audit_logger=audit_logger,
                artifact_store=artifact_store,
                use_tree=_conversation_tree_enabled_from_settings(self._settings),
            )
            # Let the bus listener finish dispatching events published just
            # before completion (e.g. final usage) so they land in the log.
            await recorder.drain_pending()
            conclusion = recorder.flush_verdict() or (result.content or "")

            # Persist final messages and conversation tree for future
            # multi-turn continuation and branching.
            if result.final_state is not None:
                update_kwargs: dict[str, Any] = {
                    "messages_json": serialize_messages(result.final_state.messages),
                }
                try:
                    update_kwargs["artifact_snapshot"] = json.dumps(
                        artifact_store.snapshot(), ensure_ascii=False
                    )
                    if spec.context_manager is not None:
                        update_kwargs["context_state"] = json.dumps(
                            spec.context_manager.snapshot_state(), ensure_ascii=False
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
                activator = getattr(spec.agent, "_domain_activator", None)
                if activator is not None:
                    update_kwargs["active_domains"] = sorted(activator.active_domains)
                await store.update(session_id, **update_kwargs)

            # Terminal: reserve seq → persist (stamped) → emit terminal event.
            if result.final_state is not None and result.final_state.status == "error":
                logger.warning(
                    "Agent run ended in error state for session %s: %s",
                    session_id,
                    result.final_state.termination_reason,
                )
                seq = run.log.reserve()
                await store.update(
                    session_id,
                    status="error",
                    finished_at=_time.time(),
                    error_detail="模型调用失败，请稍后重试",
                    event_seq=seq,
                )
                await recorder.emit_terminal("error", detail="模型调用失败，请稍后重试", seq=seq)
                run.status = "error"
            else:
                seq = run.log.reserve()
                await store.update(
                    session_id,
                    status="completed",
                    finished_at=_time.time(),
                    conclusion=conclusion,
                    event_seq=seq,
                )
                await store.finalize_turn_conclusion(session_id, conclusion, event_seq=seq)
                await recorder.emit_terminal("complete", conclusion=conclusion, seq=seq)
                run.status = "completed"
        except asyncio.CancelledError:
            logger.info("Agent run cancelled for session %s", session_id)
            try:
                seq = run.log.reserve()
                await store.update(
                    session_id, status="stopped", finished_at=_time.time(), event_seq=seq
                )
                if run.recorder is not None:
                    await run.recorder.emit_terminal("stopped", seq=seq)
                else:
                    run.log.seal()
            except Exception:
                logger.exception("Failed to persist stopped state for session %s", session_id)
                run.log.seal()
            run.status = "stopped"
        except Exception:
            trace_id = secrets.token_hex(8)
            logger.exception("Agent run failed for session %s (trace_id=%s)", session_id, trace_id)
            try:
                detail = "服务器内部错误，请稍后重试"
                seq = run.log.reserve()
                await store.update(
                    session_id,
                    status="error",
                    finished_at=_time.time(),
                    error_detail=detail,
                    event_seq=seq,
                )
                if run.recorder is not None:
                    await run.recorder.emit_terminal(
                        "error", detail=detail, trace_id=trace_id, seq=seq
                    )
                else:
                    run.log.seal()
            except Exception:
                logger.exception("Failed to persist error state for session %s", session_id)
                run.log.seal()
            run.status = "error"
        finally:
            run.finished_at = _time.time()
            if run.recorder is not None:
                run.recorder.stop_listening()
            # Close the model client to prevent AsyncHttpxClientWrapper.__del__
            # from scheduling a bare aclose() task that crashes with
            # "AttributeError: ... object has no attribute '_transport'".
            try:
                await spec.agent.model.close()
            except Exception:
                logger.debug("Error closing model client for session %s", session_id, exc_info=True)
            run.log.seal()


# -- Connection-side streaming ---------------------------------------------------


async def stream_run(
    run: AgentRun, reader: RunEventReader | None = None
) -> AsyncGenerator[str, None]:
    """Yield SSE lines from a run's event log.

    Detaching (closing the generator) only detaches the reader — the run
    itself keeps going. ``reader`` may be pre-created with a replay
    watermark (attach path); default is live-only (initiating connection).
    """
    if reader is None:
        reader = run.log.reader()
    try:
        async for entry in reader:
            yield entry.line
    finally:
        reader.aclose()


async def generate_sse_stream(
    *,
    agent: Any,
    session_id: str,
    task: str,
    agent_context: dict[str, Any],
    session_store: Any,
    settings: Any,
    run_manager: RunManager,
    user: str = "",
    prior_state: Any = None,
    artifact_snapshot: str = "",
    context_state: str = "",
    is_new: bool = True,
    start_step: int = 0,
    context_manager: Any = None,
    model_name: str = "",
    initial_seq: int = 0,
) -> AsyncGenerator[str, None]:
    """Start a run via *run_manager* and stream it (live-only).

    Route-facing wrapper kept as an async generator for StreamingResponse.
    """
    spec = RunSpec(
        session_id=session_id,
        user=user,
        task=task,
        agent=agent,
        context_manager=context_manager,
        model_name=model_name,
        agent_context=agent_context,
        prior_state=prior_state,
        artifact_snapshot=artifact_snapshot,
        context_state=context_state,
        is_new=is_new,
        start_step=start_step,
        initial_seq=initial_seq,
    )
    run = await run_manager.start(spec)
    async for line in stream_run(run):
        yield line
