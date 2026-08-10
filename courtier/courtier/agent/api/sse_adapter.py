"""SSEAdapter — bridges agent_loop callbacks to SSE events and session recording."""

from __future__ import annotations

import asyncio
import json
import logging
import time as _time
from dataclasses import replace
from typing import Any

from courtier.agent.core.execution_result import ExecutionResult

from ..core.event_bus import EventBus, EventSubscription
from ..core.events import AgentEvent
from ..tools.protocol import ToolProgress, ToolWithSkill
from .models import (
    StepRecord,
    SubagentRunRecord,
    SubagentThoughtRecord,
    SubagentToolRecord,
    ThoughtRecord,
    ToolInfo,
    ToolStatus,
    normalize_tool_call_classification,
)
from .session_store import SessionStore

logger = logging.getLogger(__name__)


class SSEAdapter:
    """Converts agent_loop callbacks into SSE events and session records.

    Events are pushed to an asyncio.Queue consumed by the SSE StreamingResponse.
    Session data is written to the SessionStore for historical queries.

    Usage:
        queue = asyncio.Queue()
        adapter = SSEAdapter(queue, session_store, session_id, pause_event)
        await agent.run(
            ...,
            on_step=adapter.on_step,
            on_token=adapter.on_token,
            on_tool_result=adapter.on_tool_result,
        )
    """

    def __init__(
        self,
        queue: asyncio.Queue,
        session_store: SessionStore,
        session_id: str,
        pause_event: asyncio.Event | None = None,
        start_step_index: int = 0,
        tool_registry: Any | None = None,
    ) -> None:
        self._queue = queue
        self._store = session_store
        self._session_id = session_id
        self._pause = pause_event

        self._step_index = start_step_index
        self._tool_start_times: dict[str, float] = {}
        self._tool_counter = 0
        self._current_step: StepRecord | None = None
        self._thought_counter = 0
        self._thinking_turn = 0
        self._segment_index = 0
        self._segment_type = "observe"
        self._verdict_parts: list[str] = []
        self._final_verdict_parts: list[str] = []  # never cleared — used for complete event
        self._tool_registry = tool_registry
        self._event_bus: EventBus | None = None
        self._event_bus_task: asyncio.Task | None = None
        # Sub-agent state accumulation for the current step (persisted at observe).
        self._current_subagents: dict[str, SubagentRunRecord] = {}
        self._subagent_parent_map: dict[str, str | None] = {}
        # Tracks the step index that owns the current sub-agent accumulation.
        # Needed because a text_response placeholder step can be created after
        # the sub-agent tool, but the tree still belongs to the preceding step.
        self._subagent_owner_step_index: int = 0
        # True after tool_calls: until at least one tool result arrives (or the
        # step is finalized), treat the current step as "has tools" when
        # deciding whether text_response needs a placeholder step.
        self._tool_calls_pending: bool = False
        # Turn tracking — incremented per user turn, persisted on steps and thoughts.
        self._current_turn_index: int = 0

    async def _check_pause(self) -> None:
        if self._pause:
            while self._pause.is_set():
                await asyncio.sleep(0.05)

    async def _resolve_turn_index(self) -> int:
        """Derive the current turn index (1-based) from the session's turn_step_starts."""
        session = await self._store.get(self._session_id)
        if not session or not session.turn_step_starts:
            return 1
        # Count how many turn boundaries are at or before the current step index.
        return sum(1 for s in session.turn_step_starts if s <= self._step_index)

    def _reset_subagent_state(self) -> None:
        """Clear accumulated sub-agent state after a step has been finalized.

        Called at observe time (not at the next think) so the accumulation
        window only ever grows within a step.  Clearing at think time raced
        with the direct-callback capture path: the bus listener may process
        ``think.tool_calls`` seconds after the run already delivered sub-agent
        start events via ``on_subagent_event``, wiping the parent's record.
        """
        self._current_subagents = {}
        self._subagent_parent_map = {}

    def _tool_meta_for(self, tool_name: str) -> dict[str, Any]:
        """Resolve skill and display_name for *tool_name* from the tool registry."""
        skill = ""
        display_name = None
        skill_description = ""

        if self._tool_registry is not None:
            try:
                tool = self._tool_registry.get(tool_name)
                if isinstance(tool, ToolWithSkill):
                    skill = tool.skill
                display_name = getattr(tool, "display_name", None)
                skill_description = getattr(tool, "description", "") or ""
            except KeyError:
                pass

        return {
            "skill": skill,
            "display_name": display_name,
            "skill_description": skill_description,
        }

    # -- Public API ------------------------------------------------------------

    def flush_verdict(self) -> str:
        """Return the full accumulated conclusion across all think phases.

        Uses _final_verdict_parts which is never cleared by observe,
        unlike _verdict_parts which is cleared per-step for step-level verdicts.
        """
        verdict = "".join(self._final_verdict_parts)
        self._final_verdict_parts.clear()
        self._verdict_parts.clear()
        return verdict

    def start_listening(self, event_bus: EventBus) -> None:
        """Subscribe to *event_bus* and dispatch events to legacy callbacks.

        This allows the adapter to be driven entirely by events while still
        producing the legacy SSE output expected by the frontend.
        """
        self._event_bus = event_bus
        self._event_subscription = event_bus.subscribe(session_id=self._session_id)
        self._event_bus_task = asyncio.create_task(
            self._event_bus_listener(self._event_subscription)
        )

    def stop_listening(self) -> None:
        """Stop the event-bus listener and remove the subscription."""
        if self._event_bus_task is not None:
            self._event_bus_task.cancel()
            self._event_bus_task = None
        if self._event_bus is not None and hasattr(self, "_event_subscription"):
            self._event_bus.unsubscribe(self._event_subscription)
        self._event_bus = None

    async def _event_bus_listener(self, subscription: EventSubscription) -> None:
        """Background task: read AgentEvents and dispatch to handlers."""
        try:
            async for event in subscription:
                await self._dispatch_event(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Event bus listener failed for session %s", self._session_id)

    async def _dispatch_event(self, event: AgentEvent) -> None:
        """Map an AgentEvent to the corresponding legacy callback."""
        payload = event.payload
        event_type = event.type

        if event_type == "state.transition":
            to = payload.get("to")
            reason = payload.get("reason", "")
            if to == "thinking" and reason == "text_response":
                await self.on_step("think", "text_response")
            elif to == "observing":
                await self.on_step("observe", "results_collected")
            elif to == "act":
                # Act events carry a structured tools list alongside the
                # legacy "executing: ..." reason string.
                await self._check_pause()
                act_payload: dict[str, Any] = {"type": "act", "detail": reason}
                if payload.get("tools"):
                    act_payload["tools"] = payload["tools"]
                await self._emit_sse(act_payload)
        elif event_type == "think.tool_calls":
            # Structured tool-call announcement from the loop (the bare
            # "tool_calls" transition reason carries no names).
            names = payload.get("names") or []
            await self._handle_think_tool_calls([str(n) for n in names])
        elif event_type == "think.text_response":
            await self.on_step("think", "text_response")
        elif event_type == "context.compacted":
            # Mirror of the legacy on_step("compact", ...) callback — route
            # through on_step so the SSE shape stays defined in one place.
            await self.on_step("compact", str(payload.get("detail", "")))
        elif event_type == "context.compacting":
            # Compaction started; the summarization LLM call may take a
            # while — the frontend shows an in-progress indicator.
            await self.on_step("compacting", str(payload.get("detail", "")))
        elif event_type == "llm.usage":
            # Structured path: apply token counts directly instead of the
            # legacy "prompt,completion" string round-trip.
            await self._apply_usage(
                payload.get("prompt_tokens", 0),
                payload.get("completion_tokens", 0),
            )
        elif event_type == "llm.token":
            text = payload.get("text")
            if text:
                await self.on_token(text)
        elif event_type == "llm.content_token":
            text = payload.get("text")
            if text:
                await self.on_content_token(text)
        elif event_type == "tool.start":
            name = payload.get("name")
            if name:
                await self.on_tool_start(name)
        elif event_type == "tool.progress":
            name = payload.get("name")
            progress = payload.get("progress")
            if name and progress is not None:
                await self.on_tool_progress(name, progress)
        elif event_type in ("tool.result", "tool.error"):
            name = payload.get("name")
            summary = payload.get("summary", "")
            # Reconstruct a minimal ExecutionResult for the existing handler.
            success = payload.get("success", event_type == "tool.result")
            issue_counts = payload.get("issue_counts")
            result = ExecutionResult(
                success=success,
                actor_type="tool",
                actor_name=name or "unknown",
                error=payload.get("error"),
                metadata=({"issue_counts": issue_counts} if isinstance(issue_counts, dict) else {}),
            )
            await self.on_tool_result(name or "unknown", result, summary)
        elif event_type == "guard.triggered":
            await self._emit_sse(
                {
                    "type": "guard_triggered",
                    "layer": payload.get("layer"),
                    "guardName": payload.get("guard_name"),
                    "action": payload.get("action"),
                    "reason": payload.get("reason"),
                }
            )
        elif event_type == "hint.injected":
            await self._emit_sse(
                {
                    "type": "hint_injected",
                    "hintType": payload.get("hint_type"),
                    "content": payload.get("content"),
                }
            )
        elif event_type == "model.selected":
            await self._emit_sse(
                {
                    "type": "model_selected",
                    "model": payload.get("model"),
                    "backend": payload.get("backend"),
                    "strategy": payload.get("strategy"),
                }
            )
        elif event_type == "model.fallback":
            await self._emit_sse(
                {
                    "type": "model_fallback",
                    "model": payload.get("model"),
                    "backend": payload.get("backend"),
                    "reason": payload.get("reason"),
                }
            )
        elif event_type == "loop.completed":
            await self._emit_sse(
                {
                    "type": "loop_completed",
                    "status": payload.get("status"),
                    "terminationReason": payload.get("termination_reason"),
                    "totalSteps": payload.get("total_steps"),
                }
            )
        elif event_type == "subagent.event":
            sub_event = payload.get("event")
            if sub_event is not None:
                await self.on_subagent_event(sub_event)

    # -- Callbacks ------------------------------------------------------------

    async def on_step(self, event: str, detail: str) -> None:
        await self._check_pause()

        if event == "think":
            await self._handle_think(detail)
        elif event == "act":
            await self._emit_sse({"type": "act", "detail": detail})
        elif event == "observe":
            await self._handle_observe()
        elif event == "compact":
            await self._emit_sse({"type": "context_compacted", "detail": detail})
        elif event == "compacting":
            await self._emit_sse({"type": "context_compacting"})
        elif event == "usage":
            await self._handle_usage(detail)
        # "stream" / "parse" events are internal, not sent to frontend

    async def on_token(self, token: str) -> None:
        await self._check_pause()

        self._thought_counter += 1
        turn_index = await self._resolve_turn_index()
        thought = ThoughtRecord(
            id=self._thought_counter,
            text=token,
            turn=self._thinking_turn,
            timestamp=_time.time() * 1000,
            segment_index=self._segment_index,
            segment_type=self._segment_type,
            step_index=self._step_index if self._current_step else None,
            turn_index=turn_index,
        )
        await self._store.add_thought(self._session_id, thought)

        # Log the start of each thinking phase so the streaming reasoning
        # process is traceable in structured_events.jsonl.
        if self._thought_counter == 1 or self._thought_counter % 50 == 0:
            logger.debug(
                "Thinking token #%d (turn %d): %r...",
                self._thought_counter,
                self._thinking_turn,
                token[:40],
            )

        # NOTE: This emits {"type": "token"} — parent-level reasoning tokens.
        # Sub-agent tokens use {"type": "subagent_token"} (see on_subagent_event).
        # The frontend distinguishes these in handleEvent() by event type.
        await self._emit_sse({"type": "token", "text": token})

    async def on_content_token(self, token: str) -> None:
        await self._check_pause()
        self._verdict_parts.append(token)
        self._final_verdict_parts.append(token)
        await self._emit_sse({"type": "conclusion_token", "text": token})

    async def on_tool_start(self, tool_name: str) -> None:
        await self._check_pause()
        self._tool_start_times[tool_name] = _time.time()
        await self._emit_sse({"type": "tool_start", "name": tool_name})

    async def on_tool_progress(self, tool_name: str, progress: ToolProgress) -> None:
        await self._check_pause()
        await self._emit_sse(
            {
                "type": "tool_progress",
                "name": tool_name,
                "progress": progress,
            }
        )

    async def on_tool_result(self, tool_name: str, result: Any, summary: str) -> None:
        await self._check_pause()

        self._segment_index += 1
        self._segment_type = "tool_result"

        now = _time.time()
        start = self._tool_start_times.get(tool_name, now)
        duration = round(now - start, 1)
        metadata = getattr(result, "metadata", None)
        classification = normalize_tool_call_classification(metadata)
        meta = self._tool_meta_for(tool_name)
        issue_counts = metadata.get("issue_counts") if metadata else None
        if not isinstance(issue_counts, dict):
            issue_counts = None

        # Determine status from result.  Post-normalization results are
        # ExecutionResult; anything else defensively maps to "ok".
        status: ToolStatus = "ok"
        if isinstance(result, ExecutionResult) and not result.success:
            status = "error"

        # Build detail from result data so frontend can display it
        detail = self._build_detail_data(result)

        self._tool_counter += 1
        tool_id = f"tool-{self._tool_counter}"

        tool_info = ToolInfo(
            name=tool_name,
            skill=meta["skill"],
            display_name=meta["display_name"],
            skill_description=meta["skill_description"],
            status=status,
            duration=duration,
            summary=summary,
            id=tool_id,
            detail=detail,
            call_kind=classification["call_kind"],
            call_scope=classification["call_scope"],
            subagent_name=classification["subagent_name"],
            parent_subagent_name=(metadata.get("parent_subagent_name") if metadata else None),
            handle_id=metadata.get("handle_id") if metadata else None,
            parent_handle_id=metadata.get("parent_handle_id") if metadata else None,
            issue_counts=issue_counts,
        )
        await self._store.add_tool_info(self._session_id, tool_info)

        # Keep the in-memory current step in sync with the store so later
        # decisions (e.g. whether to create a placeholder text-response step)
        # see the latest tools list.
        if self._current_step is not None:
            self._current_step = replace(
                self._current_step, tools=self._current_step.tools + [tool_info]
            )
            self._tool_calls_pending = False

        sse_payload: dict[str, Any] = {
            "type": "tool_result",
            "id": tool_id,
            "name": tool_name,
            "skill": tool_info.skill,
            "skillDescription": tool_info.skill_description,
            "status": status,
            "duration": duration,
            "summary": summary,
            "displayName": tool_info.display_name,
            "callKind": tool_info.call_kind,
            "callScope": tool_info.call_scope,
            "subagentName": tool_info.subagent_name,
            "handleId": tool_info.handle_id,
            "parentHandleId": tool_info.parent_handle_id,
        }
        if detail is not None:
            sse_payload["detail_data"] = detail
        if issue_counts is not None:
            sse_payload["issueCounts"] = issue_counts

        await self._emit_sse(sse_payload)

    async def on_subagent_event(self, event: Any) -> None:
        """Handle a SubAgentStreamEvent by emitting the corresponding SSE event
        and accumulating state for historical persistence.
        """
        await self._check_pause()

        # Import here to avoid circular dependency
        from ..agents.subagent.events import SubAgentStreamEvent

        if not isinstance(event, SubAgentStreamEvent):
            return

        kind = event.kind
        handle_id = event.handle_id if event.handle_id is not None else event.subagent_name
        parent_handle_id = event.parent_handle_id

        # -- State capture for historical rendering --
        if kind == "start" and handle_id:
            new_run = SubagentRunRecord(
                name=event.subagent_name,
                handle_id=handle_id,
                parent_handle_id=parent_handle_id,
                task=event.task or "",
                status="running",
            )
            self._current_subagents[handle_id] = new_run
            self._subagent_parent_map[handle_id] = parent_handle_id

        elif kind in ("token", "think") and handle_id:
            run = self._current_subagents.get(handle_id)
            if run and event.text:
                if kind == "think" and event.text == "text_response":
                    # New thought block
                    next_id = len(run.thoughts) + 1
                    self._current_subagents[handle_id] = replace(
                        run,
                        thoughts=run.thoughts + [SubagentThoughtRecord(id=next_id, text="")],
                    )
                elif run.thoughts:
                    # Append to last thought block
                    last = run.thoughts[-1]
                    self._current_subagents[handle_id] = replace(
                        run,
                        thoughts=run.thoughts[:-1] + [replace(last, text=last.text + event.text)],
                    )
                else:
                    # First token without a text_response boundary
                    self._current_subagents[handle_id] = replace(
                        run,
                        thoughts=[SubagentThoughtRecord(id=1, text=event.text)],
                    )

        elif kind == "tool_result" and handle_id:
            run = self._current_subagents.get(handle_id)
            if run and event.tool_name:
                raw_status = event.tool_status or "done"
                valid_statuses = {"pending", "running", "done", "ok", "error", "warning"}
                status = raw_status if raw_status in valid_statuses else "done"
                tool = SubagentToolRecord(
                    name=event.tool_name,
                    status=status,  # type: ignore[arg-type]
                    duration=event.tool_duration or 0.0,
                    summary=event.tool_summary or "",
                    handle_id=event.handle_id,
                    issue_counts=event.tool_issue_counts,
                )
                self._current_subagents[handle_id] = replace(run, tools=run.tools + [tool])

        elif kind == "conclusion" and handle_id:
            run = self._current_subagents.get(handle_id)
            if run:
                self._current_subagents[handle_id] = replace(
                    run,
                    conclusion=(run.conclusion or "") + (event.text or ""),
                )

        elif kind == "end" and handle_id:
            run = self._current_subagents.get(handle_id)
            if run:
                result = event.result
                is_error = isinstance(result, dict) and result.get("status") == "error"
                self._current_subagents[handle_id] = replace(
                    run,
                    status="error" if is_error else "completed",
                    error=(result.get("error", "") if isinstance(result, dict) else ""),
                )

        # -- SSE emission (unchanged) --
        if kind == "start":
            await self._emit_sse(
                {
                    "type": "subagent_start",
                    "name": event.subagent_name,
                    "displayName": event.display_name,
                    "task": event.task,
                    "parentSubagentName": event.parent_subagent_name,
                    "handleId": event.handle_id,
                    "parentHandleId": event.parent_handle_id,
                }
            )
        elif kind == "think":
            await self._emit_sse(
                {
                    "type": "subagent_think",
                    "name": event.subagent_name,
                    "text": event.text,
                    "parentSubagentName": event.parent_subagent_name,
                    "handleId": event.handle_id,
                    "parentHandleId": event.parent_handle_id,
                }
            )
        elif kind == "token":
            await self._emit_sse(
                {
                    "type": "subagent_token",
                    "name": event.subagent_name,
                    "text": event.text,
                    "parentSubagentName": event.parent_subagent_name,
                    "handleId": event.handle_id,
                    "parentHandleId": event.parent_handle_id,
                }
            )
        elif kind == "tool_result":
            tool_meta = self._tool_meta_for(event.tool_name or "")
            payload: dict[str, Any] = {
                "type": "subagent_tool_result",
                "name": event.subagent_name,
                "displayName": tool_meta["display_name"],
                "toolName": event.tool_name,
                "toolStatus": event.tool_status,
                "toolDuration": event.tool_duration,
                "toolSummary": event.tool_summary,
                "parentSubagentName": event.parent_subagent_name,
                "handleId": event.handle_id,
                "parentHandleId": event.parent_handle_id,
            }
            if event.tool_issue_counts is not None:
                payload["issueCounts"] = event.tool_issue_counts
            await self._emit_sse(payload)
        elif kind == "conclusion":
            if event.text:
                await self._emit_sse(
                    {
                        "type": "subagent_conclusion",
                        "name": event.subagent_name,
                        "text": event.text,
                        "parentSubagentName": event.parent_subagent_name,
                        "handleId": event.handle_id,
                        "parentHandleId": event.parent_handle_id,
                    }
                )
        elif kind == "end":
            await self._emit_sse(
                {
                    "type": "subagent_end",
                    "name": event.subagent_name,
                    "result": event.result,
                    "parentSubagentName": event.parent_subagent_name,
                    "handleId": event.handle_id,
                    "parentHandleId": event.parent_handle_id,
                }
            )

    # -- Internal handlers ----------------------------------------------------

    async def _handle_think(self, detail: str) -> None:
        """Legacy entry point: parse the "tool_calls: ..." detail string."""
        if detail.startswith("tool_calls:"):
            names_str = detail[len("tool_calls:") :].strip()
            names = [n.strip() for n in names_str.split(",") if n.strip()]
            await self._handle_think_tool_calls(names)
            return

        self._thinking_turn += 1
        # Create a placeholder step for free-form text responses, mirroring
        # the frontend runtime.  This keeps parent-level reasoning tokens
        # that arrive after a tool/sub-agent call in their own step, so
        # history rendering matches the streaming layout.
        if self._current_step is None or self._current_step.tools or self._tool_calls_pending:
            self._step_index += 1
            turn_index = await self._resolve_turn_index()
            self._current_step = StepRecord(
                index=self._step_index,
                label="",
                skill="",
                turn_index=turn_index,
                start_segment_index=self._segment_index,
            )
            self._tool_calls_pending = False
            await self._store.add_step(self._session_id, self._current_step)
        await self._emit_sse({"type": "think", "detail": "text_response", "textResponse": True})

    async def _handle_think_tool_calls(self, names: list[str]) -> None:
        """Create a step for announced tool calls and notify the frontend."""
        self._step_index += 1
        # Only retarget the owner index here — the accumulated sub-agent
        # state is cleared at observe time (see _reset_subagent_state), so a
        # late/bus-backed think event can never wipe runs already captured
        # for this step.
        self._subagent_owner_step_index = self._step_index
        turn_index = await self._resolve_turn_index()
        meta: dict[str, Any] = (
            self._tool_meta_for(names[0]) if names else {"skill": "", "display_name": None}
        )
        self._current_step = StepRecord(
            index=self._step_index,
            label=", ".join(names),
            skill=meta["skill"],
            turn_index=turn_index,
            start_segment_index=self._segment_index,
        )
        self._tool_start_times = {name: _time.time() for name in names}
        self._tool_calls_pending = True
        await self._store.add_step(self._session_id, self._current_step)

        await self._emit_sse(
            {
                "type": "think",
                # Legacy wire field kept for older consumers; prefer toolCalls.
                "detail": f"tool_calls:{','.join(names)}",
                "toolCalls": names,
                # Chinese display names per tool, so pending/running tool
                # cards can render them before the result arrives.
                "displayNames": {name: self._tool_meta_for(name)["display_name"] for name in names},
            }
        )

    async def _handle_observe(self) -> None:
        # Flush accumulated verdict text
        if self._verdict_parts:
            verdict = "".join(self._verdict_parts)
            if self._current_step:
                await self._store.set_verdict(self._session_id, verdict)
                # Tell the live stream what persistence already knows: this
                # text is the step's intermediate verdict, not part of the
                # final conclusion. Emitted before "observe" so the frontend
                # finalizes the step verdict before closing the step frame.
                await self._emit_sse(
                    {
                        "type": "step_verdict",
                        "stepIndex": self._current_step.index,
                        "text": verdict,
                    }
                )
            else:
                logger.debug(
                    "Discarding pre-step verdict tokens (%d chars): %r...",
                    len(verdict),
                    verdict[:80],
                )
            self._verdict_parts.clear()

        self._segment_index += 1
        self._segment_type = "observe"
        self._tool_calls_pending = False

        # Persist sub-agent tree and segment boundary for historical rendering.
        # The owner may be the preceding tool step even when a text_response
        # placeholder step has been created after it.
        owner_step_index = self._subagent_owner_step_index or self._step_index
        if self._current_step and owner_step_index > 0:
            await self._store.finalize_step(
                self._session_id,
                owner_step_index,
                subagents=self._build_subagent_tree(),
                end_segment_index=self._segment_index,
            )

        # The step's tree is now persisted — clear accumulation state so the
        # next step starts fresh.  Done here (not at the next think) because
        # sub-agent events for the step may still be in flight until observe.
        self._reset_subagent_state()

        await self._emit_sse({"type": "observe"})

    async def _handle_usage(self, detail: str) -> None:
        """Legacy entry point: parse the "prompt,completion" detail string."""
        try:
            tin_str, tout_str = detail.split(",", 1)
            tokens_in = int(tin_str.strip())
            tokens_out = int(tout_str.strip())
        except (ValueError, TypeError):
            logger.debug("Unparseable usage detail: %r", detail)
            return
        await self._apply_usage(tokens_in, tokens_out)

    async def _apply_usage(self, tokens_in: int, tokens_out: int) -> None:
        # Update session token counts
        session = await self._store.get(self._session_id)
        if session:
            await self._store.update(
                self._session_id,
                tokens_in=session.tokens_in + tokens_in,
                tokens_out=session.tokens_out + tokens_out,
            )

        await self._emit_sse(
            {
                "type": "usage",
                "tokensIn": tokens_in,
                "tokensOut": tokens_out,
            }
        )

    # -- Sub-agent tree construction ------------------------------------------

    def _build_subagent_tree(self) -> list[SubagentRunRecord]:
        """Build a nested sub-agent tree from the flat _current_subagents dict.

        Top-level runs (parent_handle_id is None) become roots.  Children are
        nested under their parent by matching parent_handle_id to handle_id.
        """
        if not self._current_subagents:
            return []

        # Partition into roots and children.
        children_by_parent: dict[str, list[SubagentRunRecord]] = {}
        roots: list[SubagentRunRecord] = []
        for handle_id, run in self._current_subagents.items():
            parent = self._subagent_parent_map.get(handle_id)
            if parent and parent in self._current_subagents:
                children_by_parent.setdefault(parent, []).append(run)
            else:
                roots.append(run)

        def nest(run: SubagentRunRecord) -> SubagentRunRecord:
            kids = children_by_parent.get(run.handle_id, [])
            if not kids:
                return run
            return replace(run, children=[nest(k) for k in kids])

        return [nest(r) for r in roots]

    # -- Helpers --------------------------------------------------------------

    async def _emit_sse(self, data: dict[str, Any]) -> None:
        line = f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
        try:
            self._queue.put_nowait(("event", line))
        except asyncio.QueueFull:
            logger.warning("SSE queue full, dropping event: %s", data.get("type", "unknown"))

    #: Max top-level keys in a dict before truncation in structured detail.
    _MAX_STRUCTURED_KEYS = 30
    #: Max length of a single string value in structured detail (chars).
    _MAX_STRUCTURED_STRING_LEN = 500

    @classmethod
    def _build_detail_data(cls, result: Any) -> dict[str, Any] | None:
        """Build ToolDetail from an ExecutionResult."""
        if not isinstance(result, ExecutionResult) or not result.success:
            return None
        data = result.raw_data

        if data is None:
            return None

        if isinstance(data, str):
            return {"type": "markdown", "content": data}

        if isinstance(data, dict):
            if data.get("__persisted_output__"):
                return {
                    "type": "structured",
                    "data": {
                        "preview": data.get("preview", ""),
                        "data_shape": data.get("data_shape", {}),
                    },
                }
            # Serialize safely — skip non-serializable values and cap size.
            safe: dict[str, Any] = {}
            truncated_keys = 0
            for k, v in data.items():
                if len(safe) >= cls._MAX_STRUCTURED_KEYS:
                    truncated_keys = len(data) - len(safe)
                    break
                if isinstance(v, str):
                    if len(v) > cls._MAX_STRUCTURED_STRING_LEN:
                        safe[k] = v[: cls._MAX_STRUCTURED_STRING_LEN] + "…"
                    else:
                        safe[k] = v
                elif isinstance(v, (int, float, bool, type(None), list, dict)):
                    safe[k] = v
                else:
                    safe[k] = str(v)
            if truncated_keys > 0:
                safe["_truncated_keys"] = truncated_keys
            return {"type": "structured", "data": safe}

        if isinstance(data, list):
            return {
                "type": "structured",
                "data": {"items": data[:100], "total": len(data)},
            }

        # Scalar
        return {"type": "structured", "data": {"value": data}}
