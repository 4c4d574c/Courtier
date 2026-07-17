"""AgentLoop — the core invariant. Stays simple as the system grows."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from opentelemetry import trace as otel_trace
from opentelemetry.trace import Status, StatusCode

from courtier.agent.core.execution_result import ExecutionResult
from courtier.common.behavioral_rules import PERIODIC_REMINDER, PRE_TURN_REMINDER

from ..artifacts.resolver import emit_event
from ..telemetry.metrics import (
    record_agent_request,
    record_llm_call,
    record_llm_tokens,
    record_tool_execution,
    record_tool_latency,
)
from ..telemetry.tracer import AgentTracer
from ..tools.protocol import ToolProgress
from .audit_logger import AuditLogger
from .event_bus import EventBus
from .events import AgentEvent, EventType
from .guardrails import (
    BusinessArtifactProgressGuard,
    ExploreLoopGuard,
    GuardContext,
    GuardrailSystem,
)
from .guardrails.base import GuardResult
from .loop_audit import write_audit_turn
from .loop_hints import _get_ready_terminal_tools, check_and_inject_hints
from .loop_phases import execute_tools_phase, think_phase
from .state import AgentState, Message
from .state_machine import AgentStateMachine

if TYPE_CHECKING:
    from ..hooks.chain import HookChain
    from ..permissions.gate import PermissionGate
    from ..tools.registry import ToolRegistry
    from .context_manager import ContextManager
    from .model import ModelClient

logger = logging.getLogger(__name__)


def _maybe_write_audit_turn(
    audit_logger: AuditLogger | None,
    turn_index: int,
    timestamp: float,
    think: Any,
    tool_records: tuple = (),
) -> None:
    """Write the audit turn if an audit logger is configured."""
    if audit_logger is None:
        return
    write_audit_turn(
        audit_logger,
        turn_index,
        timestamp,
        think.llm_request,
        think.llm_response,
        tool_records,
    )


def _inject_reminder(
    messages: tuple[Message, ...], reminder: str
) -> tuple[Message, ...]:
    """Append *reminder*, removing any earlier occurrence of the same reminder.

    Pre-turn and periodic reminders are injected every turn.  Without
    deduplication they would accumulate linearly with turn count because
    the latest message is usually a tool result, not the previous reminder.
    Reminder messages are tagged with ``source="reminder"`` so they are not
    confused with genuine user messages that happen to contain the same text.
    """
    msgs = [
        m for m in messages
        if not (m.role == "user" and m.source == "reminder" and m.content == reminder)
    ]
    msgs.append(Message(role="user", content=reminder, source="reminder"))
    return tuple(msgs)


async def _terminate_loop_step(
    *,
    reason: str,
    on_step: Any | None,
    audit_logger: AuditLogger | None,
    turn_index: int,
    timestamp: float,
    think: Any,
    tool_records: tuple,
) -> None:
    """Call on_step (if configured) and write audit turn for a loop termination guard."""
    if on_step:
        await on_step("loop", reason)
    _maybe_write_audit_turn(audit_logger, turn_index, timestamp, think, tool_records)


@dataclass
class _ThinkPhaseOutcome:
    """Result of one think-phase iteration."""

    state: AgentState
    think: Any
    turn_index: int
    timestamp: float
    break_loop: bool
    prompt_tokens: int
    completion_tokens: int


@dataclass
class _ToolPhaseOutcome:
    """Result of one tool-phase iteration."""

    state: AgentState
    think: Any
    break_loop: bool
    consecutive_exploratory: int


async def _run_think_phase(
    *,
    current_state: AgentState,
    hooks: Any | None,
    guardrail_system: Any | None,
    state_machine: AgentStateMachine,
    model: ModelClient,
    tool_registry: Any | None,
    context_manager: Any | None,
    recent_reasoning: list[str],
    on_step: Callable[[str, str], Awaitable[None]],
    on_token: Callable[[str], Awaitable[None]],
    on_content_token: Callable[[str], Awaitable[None]],
    _publish: Callable[..., Awaitable[None]],
    tracer: AgentTracer,
    audit_logger: AuditLogger | None,
    agent_name: str,
) -> _ThinkPhaseOutcome:
    """Run a single think phase: hook, guards, reminder, model call, transitions.

    Returns the updated state, the think result, and flags that tell the main
    loop whether to break or return immediately.
    """
    turn_index = current_state.current_step
    timestamp = time.time()
    prompt_tokens = 0
    completion_tokens = 0

    # Hook: pre_think
    if hooks:
        current_state = await hooks.run("pre_think", current_state)
        if current_state.is_terminal():
            return _ThinkPhaseOutcome(
                state=current_state,
                think=None,
                turn_index=turn_index,
                timestamp=timestamp,
                break_loop=True,
                prompt_tokens=0,
                completion_tokens=0,
            )

    # Guardrail: input layer
    if guardrail_system is not None:
        guard_result = await guardrail_system.check(
            "input",
            GuardContext(
                state=current_state,
                messages=current_state.messages,
            ),
        )
        if guard_result.action == "block":
            reason = f"guardrail:{guard_result.guard_name}:{guard_result.reason}"
            current_state = await state_machine.transition_async(
                current_state, "blocked", reason
            )
            return _ThinkPhaseOutcome(
                state=current_state,
                think=None,
                turn_index=turn_index,
                timestamp=timestamp,
                break_loop=True,
                prompt_tokens=0,
                completion_tokens=0,
            )

    # Pre-turn reminder — injected before every think phase to
    # prevent first-tool-call paralysis and mid-conversation
    # deliberation loops.  Uses role="user" because many API
    # providers reject interleaved system messages.
    current_state = current_state.model_copy(
        update={"messages": _inject_reminder(current_state.messages, PRE_TURN_REMINDER)}
    )

    # State transition: idle/thinking -> thinking
    current_state = await state_machine.transition_async(
        current_state, "thinking", "turn_start"
    )

    # Publish LLM request metadata before invoking the model.
    tools_schemas = (
        tool_registry.get_schemas(hide_debug_for_task_agents=True)
        if tool_registry
        else None
    )
    await _publish(
        "llm.request",
        {
            "model": getattr(model, "model_name", "unknown"),
            "temperature": getattr(model, "temperature", 0.0),
            "message_count": len(current_state.messages),
            "tool_count": len(tools_schemas) if tools_schemas else 0,
        },
    )

    # THINK phase with OTel LLM span
    with tracer.llm_span(
        model=model.model_name,
        temperature=getattr(model, "temperature", 0.0),
    ) as llm_span:
        think = await think_phase(
            state=current_state,
            model=model,
            tool_registry=tool_registry,
            context_manager=context_manager,
            recent_reasoning=recent_reasoning,
            on_step=on_step,
            on_token=on_token,
            on_content_token=on_content_token,
        )
        # Record prompt/completion as span events
        llm_request_msgs = think.llm_request.messages
        if llm_request_msgs:
            last_msg = llm_request_msgs[-1]
            content = (
                str(last_msg.get("content", ""))
                if isinstance(last_msg, dict)
                else str(last_msg)
            )
            tracer.log_prompt(llm_span, content)
        if think.llm_response.content:
            tracer.log_completion(
                llm_span,
                think.llm_response.content,
                think.llm_response.finish_reason,
            )
        if think.llm_response.usage:
            usage = think.llm_response.usage
            tracer.set_token_usage(
                llm_span,
                usage.get("prompt_tokens", 0),
                usage.get("completion_tokens", 0),
            )
        if think.failed:
            llm_span.set_status(
                Status(StatusCode.ERROR, "LLM call failed")
            )
    current_state = think.state
    recent_reasoning[:] = think.recent_reasoning

    # Publish LLM response metadata.
    await _publish(
        "llm.response",
        {
            "model": think.llm_request.model or "unknown",
            "status": current_state.status,
            "has_content": think.llm_response.content is not None,
            "tool_call_count": len(think.llm_response.tool_calls or []),
            "usage": think.llm_response.usage,
            "finish_reason": think.llm_response.finish_reason,
        },
    )

    # State transition: thinking -> waiting_for_tool / completed / error
    if think.failed:
        current_state = await state_machine.transition_async(
            current_state,
            "error",
            current_state.termination_reason or "model_error",
        )
    elif think.reasoning_loop:
        current_state = await state_machine.transition_async(
            current_state, "completed", "reasoning_loop_detected"
        )
    elif current_state.tool_calls:
        current_state = await state_machine.transition_async(
            current_state, "waiting_for_tool", "tool_calls"
        )
        # Structured tool-call announcement — replaces the legacy
        # on_step("think", "tool_calls: ...") string for bus consumers.
        # (Without it the adapter only saw the bare "tool_calls" transition
        # reason and mis-mapped the turn to a text_response step.)
        await _publish(
            "think.tool_calls",
            {"names": [tc.name for tc in current_state.tool_calls]},
        )
    else:
        current_state = await state_machine.transition_async(
            current_state, "completed", "text_response"
        )
        await _publish("think.text_response", {})

    # LLM metrics — record the call regardless of whether the provider
    # returned usage; only token counts depend on usage being present.
    record_llm_call(
        model=think.llm_request.model or "unknown",
        agent_name=agent_name,
    )
    if think.llm_response.usage:
        usage = think.llm_response.usage
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)
        record_llm_tokens(
            model=think.llm_request.model or "unknown",
            input_tokens=prompt_tokens,
            output_tokens=completion_tokens,
        )

    # Model error: write the audit turn here; all remaining run finalization
    # (loop.completed event, audit finalize, request metrics, span attributes)
    # happens once in agent_loop's shared tail after the loop breaks.
    if current_state.status == "error":
        _maybe_write_audit_turn(audit_logger, turn_index, timestamp, think)
        return _ThinkPhaseOutcome(
            state=current_state,
            think=think,
            turn_index=turn_index,
            timestamp=timestamp,
            break_loop=True,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

    # If model returned no tool calls or reasoning loop detected, loop ends
    if current_state.is_terminal():
        _maybe_write_audit_turn(audit_logger, turn_index, timestamp, think)
        return _ThinkPhaseOutcome(
            state=current_state,
            think=think,
            turn_index=turn_index,
            timestamp=timestamp,
            break_loop=True,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

    return _ThinkPhaseOutcome(
        state=current_state,
        think=think,
        turn_index=turn_index,
        timestamp=timestamp,
        break_loop=False,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )


async def _run_tool_phase(
    *,
    current_state: AgentState,
    think: Any,
    guardrail_system: Any | None,
    permissions: Any | None,
    state_machine: AgentStateMachine,
    tool_registry: Any | None,
    context_manager: Any | None,
    artifact_store: Any | None,
    audit_logger: AuditLogger | None,
    on_step: Callable[[str, str], Awaitable[None]],
    on_tool_result: Callable[[str, ExecutionResult, str], Awaitable[None]] | None,
    on_tool_start: Callable[[str], Awaitable[None]] | None,
    on_tool_progress: Callable[[str, Any], Awaitable[None]] | None,
    _publish: Callable[..., Awaitable[None]],
    tracer: AgentTracer,
    agent_name: str,
    session_id: str,
    turn_index: int,
    timestamp: float,
    consecutive_exploratory: int,
    event_bus: EventBus | None,
    hooks: Any | None,
) -> _ToolPhaseOutcome:
    """Run a single tool phase: guards, permission gate, execute, observe, hooks.

    Returns the updated state and the latest consecutive exploratory count.
    """
    # Guardrail: output layer
    if guardrail_system is not None:
        guard_result = await guardrail_system.check(
            "output",
            GuardContext(
                state=current_state,
                response_text=think.llm_response.content,
                tool_calls=current_state.tool_calls,
            ),
        )
        if guard_result.action == "block":
            reason = f"guardrail:{guard_result.guard_name}:{guard_result.reason}"
            current_state = await state_machine.transition_async(
                current_state, "blocked", reason
            )
            return _ToolPhaseOutcome(
                state=current_state,
                think=think,
                break_loop=True,
                consecutive_exploratory=consecutive_exploratory,
            )

    # GATE: permission check
    if permissions:
        denied_tools = [
            tool_call.name
            for tool_call in current_state.tool_calls
            if not permissions.allow(tool_call)
        ]
        if denied_tools:
            reason = f"Permission denied: {', '.join(denied_tools)}"
            current_state = await state_machine.transition_async(
                current_state,
                "blocked",
                reason,
            )

    if current_state.is_terminal():
        _maybe_write_audit_turn(audit_logger, turn_index, timestamp, think)
        return _ToolPhaseOutcome(
            state=current_state,
            think=think,
            break_loop=True,
            consecutive_exploratory=consecutive_exploratory,
        )

    # Guardrail: tool layer
    if guardrail_system is not None and current_state.tool_calls:
        guard_result = await guardrail_system.check(
            "tool",
            GuardContext(
                state=current_state,
                tool_calls=current_state.tool_calls,
            ),
        )
        if guard_result.action == "block":
            reason = f"guardrail:{guard_result.guard_name}:{guard_result.reason}"
            current_state = await state_machine.transition_async(
                current_state, "blocked", reason
            )
            return _ToolPhaseOutcome(
                state=current_state,
                think=think,
                break_loop=True,
                consecutive_exploratory=consecutive_exploratory,
            )

    # ACT + OBSERVE: execute tools, collect results
    if current_state.tool_calls and tool_registry is None:
        record_agent_request(
            agent_name=agent_name,
            status="error",
        )
        current_state = await state_machine.transition_async(
            current_state,
            "error",
            "missing_tool_registry",
        )
        return _ToolPhaseOutcome(
            state=current_state,
            think=think,
            break_loop=True,
            consecutive_exploratory=consecutive_exploratory,
        )

    # Publish the act transition unconditionally — the state machine does not
    # cover "act", and pure event-bus consumers (SSE) have no other source
    # for it. The legacy callback (when set) is invoked separately.
    names = ", ".join(tc.name for tc in current_state.tool_calls)
    await _publish(
        "state.transition",
        {
            "to": "act",
            "reason": f"executing: {names}",
            "tools": [tc.name for tc in current_state.tool_calls],
        },
    )
    await on_step("act", f"executing: {names}")

    results, tool_records = await execute_tools_phase(
        state=current_state,
        tool_registry=tool_registry,
        context_manager=context_manager,
        artifact_store=artifact_store,
        on_tool_result=on_tool_result,
        on_tool_start=on_tool_start,
        on_tool_progress=on_tool_progress,
        audit_logger=audit_logger,
    )

    # Capture the tool calls that were actually executed before
    # add_observation clears them, so downstream tracking and guard
    # checks see the calls made this turn.
    executed_tool_calls = current_state.tool_calls
    current_state = current_state.add_observation(
        tuple(results), set_status=False
    )
    current_state = current_state.record_turn()
    current_state = await state_machine.transition_async(
        current_state, "observing", "tools_executed"
    )

    # Record tool execution spans and metrics
    for record in tool_records:
        with tracer.tool_span(
            tool_name=record.tool_name,
            parameters=record.arguments,
        ) as tool_span:
            tool_span.set_attribute(
                "gen_ai.tool.latency_ms", record.duration_ms
            )
            tool_span.set_attribute(
                "gen_ai.tool.status",
                "success" if record.result_success else "error",
            )
            tracer.set_tool_result(tool_span, record.result_data)

        record_tool_execution(
            tool_name=record.tool_name,
            status="success" if record.result_success else "error",
        )
        record_tool_latency(
            tool_name=record.tool_name,
            seconds=record.duration_ms / 1000.0,
        )

    # Guardrail: post_tool layer — detect futile exploration patterns
    # and lack of business artifact progress after tools have run.
    if guardrail_system is not None:
        guard_result = await guardrail_system.check(
            "post_tool",
            GuardContext(
                state=current_state,
                tool_calls=executed_tool_calls,
                tool_results=results,
                metadata={"artifact_store": artifact_store},
            ),
        )
        if guard_result.action == "block":
            reason = f"guardrail:{guard_result.guard_name}:{guard_result.reason}"
            current_state = await state_machine.transition_async(
                current_state, "completed", reason
            )
            await _terminate_loop_step(
                reason=reason,
                on_step=on_step, audit_logger=audit_logger,
                turn_index=turn_index, timestamp=timestamp,
                think=think, tool_records=tuple(tool_records),
            )
            return _ToolPhaseOutcome(
                state=current_state,
                think=think,
                break_loop=True,
                consecutive_exploratory=consecutive_exploratory,
            )

        # Mirror the ExploreLoopGuard's consecutive exploratory count so
        # hint injection and terminal-tool detection stay consistent.
        consecutive_exploratory = guard_result.metadata.get(
            "consecutive_exploratory", consecutive_exploratory
        )

    # -- Terminal-tool readiness guard: when a terminal tool becomes ready,
    #    proactively inject a readiness summary. If the model keeps calling
    #    exploratory tools, escalate to blocking / forced termination.
    #    Blocked-tools hints: when no terminal tool is ready but some
    #    tools with contracts exist, inject a hint about what's missing.
    current_state, force_complete_reason = await check_and_inject_hints(
        tool_registry=tool_registry,
        artifact_store=artifact_store,
        consecutive_exploratory=consecutive_exploratory,
        current_state=current_state,
        agent_name=agent_name,
        event_bus=event_bus,
        session_id=session_id,
        turn_index=turn_index,
    )
    if force_complete_reason is not None:
        # Drive the forced completion through the state machine so the
        # transition stays auditable (transition_id + state.transition event).
        current_state = await state_machine.transition_async(
            current_state, "completed", force_complete_reason
        )
        await _terminate_loop_step(
            reason=force_complete_reason,
            on_step=on_step, audit_logger=audit_logger,
            turn_index=turn_index, timestamp=timestamp,
            think=think, tool_records=tuple(tool_records),
        )
        return _ToolPhaseOutcome(
            state=current_state,
            think=think,
            break_loop=True,
            consecutive_exploratory=consecutive_exploratory,
        )

    # Gap 4: emit terminal_tool_called when a terminal tool was invoked
    if (
        consecutive_exploratory == 0
        and artifact_store is not None
        and tool_registry is not None
    ):
        ready_tools_set = set(
            _get_ready_terminal_tools(tool_registry, artifact_store)
        )
        called_terminal = [
            tc.name for tc in executed_tool_calls
            if tc.name in ready_tools_set
        ]
        if called_terminal:
            emit_event("terminal_tool_called", {
                "tools": called_terminal,
            })

    # Write audit log for this turn
    _maybe_write_audit_turn(
        audit_logger, turn_index, timestamp, think, tuple(tool_records)
    )

    # --- Context budget: Layer 2 micro-compact old tool results ---
    if context_manager:
        compacted_messages = await context_manager.micro_compact(current_state.messages)
        current_state = current_state.model_copy(
            update={"messages": compacted_messages}
        )

    # Periodic behavioral reminder — reinforces system-prompt rules
    # that drift out of attention during long conversations.
    # Injected every 5 turns (starting from turn 5).
    if turn_index > 0 and turn_index % 5 == 0:
        current_state = current_state.model_copy(
            update={"messages": _inject_reminder(current_state.messages, PERIODIC_REMINDER)}
        )

    await on_step("observe", "results_collected")

    # Hook: post_observe
    if hooks:
        current_state = await hooks.run("post_observe", current_state)

    return _ToolPhaseOutcome(
        state=current_state,
        think=think,
        break_loop=False,
        consecutive_exploratory=consecutive_exploratory,
    )


async def agent_loop(
    *,
    state: AgentState,
    model: ModelClient,
    tool_registry: ToolRegistry | None = None,
    hooks: HookChain | None = None,
    permissions: PermissionGate | None = None,
    on_step: Callable[[str, str], Awaitable[None]] | None = None,
    on_token: Callable[[str], Awaitable[None]] | None = None,
    on_content_token: Callable[[str], Awaitable[None]] | None = None,
    on_tool_result: Callable[[str, ExecutionResult, str], Awaitable[None]] | None = None,
    on_tool_start: Callable[[str], Awaitable[None]] | None = None,
    on_tool_progress: Callable[[str, ToolProgress], Awaitable[None]] | None = None,
    context_manager: ContextManager | None = None,
    audit_logger: AuditLogger | None = None,
    artifact_store: Any | None = None,
    session_id: str = "",
    agent_name: str = "",
    event_bus: EventBus | None = None,
    guardrail_system: GuardrailSystem | None = None,
) -> AgentState:
    """Agent 主循环 — think → gate → act → observe.

    on_step(event, detail): called at each phase transition.
    on_token(token): called for reasoning/chain-of-thought tokens.
    on_content_token(token): called for final content/response tokens.
    on_tool_result(tool_name, result, summary): called for each tool result.
    on_tool_start(tool_name, arguments): called when a tool begins execution.
    on_tool_progress(tool_name, chunk): called for streaming tool progress.
    context_manager: three-layer context budget control (optional).
    event_bus: optional publish/subscribe bus for AgentEvent instances. When
        provided, the loop publishes structured events and legacy callbacks are
        still invoked unless overridden by the caller.
    """
    tracer = AgentTracer()
    if not agent_name:
        agent_name = getattr(state, "agent_name", "") or "unknown"
    # Extract task from the first user message in conversation history
    _task = ""
    for msg in state.messages:
        if msg.role == "user" and msg.content:
            _task = msg.content[:500]
            break

    async def _publish(
        event_type: EventType,
        payload: dict[str, Any],
        turn_index: int | None = None,
    ) -> None:
        """Publish an AgentEvent if an event bus is configured.

        Publish failures are logged and swallowed — event delivery must
        never abort the agent run itself.
        """
        if event_bus is None:
            return
        try:
            await event_bus.publish(
                AgentEvent(
                    type=event_type,
                    session_id=session_id,
                    agent_name=agent_name,
                    turn_index=turn_index if turn_index is not None else current_state.current_step,
                    payload=payload,
                )
            )
        except Exception:
            logger.warning("Failed to publish %s event", event_type, exc_info=True)

    # Wrap legacy callbacks so every significant action is also published as
    # an event. When an event bus is present, the loop emits structured events;
    # legacy callbacks are still invoked for backwards compatibility.
    #
    # Note: state.transition events are now published by
    # ``AgentStateMachine.transition_async()``; this wrapper only handles
    # legacy callback invocation and the llm.usage event.
    async def _safe_call(callback: Callable[..., Awaitable[None]], *args: Any) -> None:
        """Invoke a legacy callback, isolating its failures from the loop.

        A raising display/consumer callback must never abort the agent run.
        """
        try:
            await callback(*args)
        except Exception:
            logger.warning(
                "Legacy callback %s raised; continuing run",
                getattr(callback, "__name__", repr(callback)),
                exc_info=True,
            )

    async def _on_step(event: str, detail: str) -> None:
        if event == "usage":
            try:
                tin_str, tout_str = detail.split(",", 1)
                prompt_tokens = int(tin_str.strip())
                completion_tokens = int(tout_str.strip())
            except (ValueError, TypeError):
                logger.warning("Malformed usage detail: %r", detail)
                return
            await _publish(
                "llm.usage",
                {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                },
            )
        if on_step is not None:
            await _safe_call(on_step, event, detail)

    async def _on_token(token: str) -> None:
        await _publish("llm.token", {"text": token, "kind": "reasoning"})
        if on_token is not None:
            await _safe_call(on_token, token)

    async def _on_content_token(token: str) -> None:
        await _publish("llm.content_token", {"text": token, "kind": "content"})
        if on_content_token is not None:
            await _safe_call(on_content_token, token)

    async def _on_tool_start(tool_name: str) -> None:
        await _publish("tool.start", {"name": tool_name})
        if on_tool_start is not None:
            await _safe_call(on_tool_start, tool_name)

    async def _on_tool_progress(tool_name: str, progress: ToolProgress) -> None:
        await _publish(
            "tool.progress",
            {"name": tool_name, "progress": progress},
        )
        if on_tool_progress is not None:
            await _safe_call(on_tool_progress, tool_name, progress)

    async def _on_tool_result(tool_name: str, result: ExecutionResult, summary: str) -> None:
        await _publish(
            "tool.result" if result.success else "tool.error",
            {
                "name": tool_name,
                "summary": summary,
                "success": result.success,
                "error": result.error,
            },
        )
        if on_tool_result is not None:
            await _safe_call(on_tool_result, tool_name, result, summary)

    async def _on_guardrail_event(result: GuardResult) -> None:
        """Publish guard.triggered events when a guard emits log/block."""
        if event_bus is None or result.action == "allow":
            return
        await _publish(
            "guard.triggered",
            {
                "layer": result.layer,
                "guard_name": result.guard_name,
                "action": result.action,
                "reason": result.reason,
                "metadata": result.metadata or {},
            },
        )

    # Explicit state machine: all status transitions go through this object.
    state_machine = AgentStateMachine(
        strict=False,
        event_bus=event_bus,
        session_id=session_id,
        agent_name=agent_name,
    )

    # Default guardrail system includes migrated loop guards. Callers can supply
    # their own GuardrailSystem to override or extend checks.
    if guardrail_system is None:
        guardrail_system = GuardrailSystem(tool_mode="block")
        guardrail_system.register(ExploreLoopGuard())
        guardrail_system.register(BusinessArtifactProgressGuard())
    guardrail_system.on_event = _on_guardrail_event

    with tracer.agent_span(agent_name, session_id=session_id, task=_task):
        # Provide session-level context to hook handlers.
        if hooks:
            hooks.set_context(agent_name=agent_name, session_id=session_id)
        current_state = state
        recent_reasoning: list[str] = []
        # Accumulate token usage for the agent span
        total_prompt_tokens = 0
        total_completion_tokens = 0
        # Track consecutive exploratory (read-only) tool calls for hint injection
        # and terminal-tool detection. The ExploreLoopGuard itself tracks null
        # results and repeated calls; we mirror the exploratory count here.
        consecutive_exploratory: int = 0
        # Reset ToolRuntimePolicy per-run counters
        if tool_registry is not None:
            tool_registry.reset_run_state()

        # A turn is recorded into the conversation tree exactly once: by the
        # tool phase right after its observation, or — when the loop ends
        # right after a think phase (final text, model error, max_steps) —
        # once at the tail below. Turns aborted before tool execution
        # (blocked / guard-forced stops) are intentionally not recorded.
        turn_pending_record = False
        try:
            while not current_state.is_terminal():
                think_outcome = await _run_think_phase(
                    current_state=current_state,
                    hooks=hooks,
                    guardrail_system=guardrail_system,
                    state_machine=state_machine,
                    model=model,
                    tool_registry=tool_registry,
                    context_manager=context_manager,
                    recent_reasoning=recent_reasoning,
                    on_step=_on_step,
                    on_token=_on_token,
                    on_content_token=_on_content_token,
                    _publish=_publish,
                    tracer=tracer,
                    audit_logger=audit_logger,
                    agent_name=agent_name,
                )
                current_state = think_outcome.state
                total_prompt_tokens += think_outcome.prompt_tokens
                total_completion_tokens += think_outcome.completion_tokens
                turn_pending_record = True
                if think_outcome.break_loop:
                    break

                tool_outcome = await _run_tool_phase(
                    current_state=current_state,
                    think=think_outcome.think,
                    guardrail_system=guardrail_system,
                    permissions=permissions,
                    state_machine=state_machine,
                    tool_registry=tool_registry,
                    context_manager=context_manager,
                    artifact_store=artifact_store,
                    audit_logger=audit_logger,
                    on_step=_on_step,
                    on_tool_result=_on_tool_result,
                    on_tool_start=_on_tool_start,
                    on_tool_progress=_on_tool_progress,
                    _publish=_publish,
                    tracer=tracer,
                    agent_name=agent_name,
                    session_id=session_id,
                    turn_index=think_outcome.turn_index,
                    timestamp=think_outcome.timestamp,
                    consecutive_exploratory=consecutive_exploratory,
                    event_bus=event_bus,
                    hooks=hooks,
                )
                current_state = tool_outcome.state
                consecutive_exploratory = tool_outcome.consecutive_exploratory
                turn_pending_record = False
                if tool_outcome.break_loop:
                    break
        except Exception as exc:
            # Last-resort containment: an unexpected failure in any phase must
            # still end in a terminal state, so the shared tail below publishes
            # loop.completed, finalizes the audit log, and records metrics.
            # CancelledError (BaseException) propagates untouched.
            logger.exception("Agent loop failed unexpectedly")
            current_state = await state_machine.transition_async(
                current_state, "error", f"internal_error: {exc}"
            )

        if turn_pending_record:
            current_state = current_state.record_turn()

        await _publish(
            "loop.completed",
            {
                "status": current_state.status,
                "termination_reason": current_state.termination_reason,
                "total_steps": current_state.current_step,
            },
        )

        if audit_logger:
            audit_logger.finalize(
                final_status=current_state.status,
                termination_reason=current_state.termination_reason,
            )

        record_agent_request(
            agent_name=agent_name,
            status=current_state.status,
        )
        # Set span attributes from accumulated state
        agent_span = otel_trace.get_current_span()
        if agent_span:
            agent_span.set_attribute("agent.iterations", current_state.current_step)
            agent_span.set_attribute("agent.final_status", current_state.status)
            agent_span.set_attribute(
                "agent.total_tokens",
                total_prompt_tokens + total_completion_tokens,
            )
            agent_span.set_attribute("agent.input_tokens", total_prompt_tokens)
            agent_span.set_attribute("agent.output_tokens", total_completion_tokens)

    return current_state
