"""AgentLoop — the core invariant. Stays simple as the system grows."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, TYPE_CHECKING

from .audit_logger import AuditLogger
from .loop_audit import write_audit_turn
from .loop_phases import think_phase, execute_tools_phase
from .loop_guards import (
    check_explore_loop,
    check_business_artifact_progress,
    update_null_tracking,
    update_tool_call_history,
    update_exploratory_tracking,
    _count_business_artifacts,
)
from .loop_hints import check_and_inject_hints, _get_ready_terminal_tools
from .state import AgentState, Message
from ..artifacts.resolver import emit_event
from ..tools.protocol import ToolProgress
from courtier.common.behavioral_rules import PERIODIC_REMINDER, PRE_TURN_REMINDER
from courtier.agent.core.execution_result import ExecutionResult
from ..telemetry.tracer import AgentTracer
from ..telemetry.metrics import (
    record_agent_request,
    record_llm_call,
    record_llm_tokens,
    record_tool_execution,
    record_tool_latency,
)
from opentelemetry import trace as otel_trace
from opentelemetry.trace import Status, StatusCode

if TYPE_CHECKING:
    from .model import ModelClient
    from .context_manager import ContextManager
    from ..hooks.chain import HookChain
    from ..permissions.gate import PermissionGate
    from ..tools.registry import ToolRegistry

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
) -> AgentState:
    """Agent 主循环 — think → gate → act → observe.

    on_step(event, detail): called at each phase transition.
    on_token(token): called for reasoning/chain-of-thought tokens.
    on_content_token(token): called for final content/response tokens.
    on_tool_result(tool_name, result, summary): called for each tool result.
    on_tool_start(tool_name, arguments): called when a tool begins execution.
    on_tool_progress(tool_name, chunk): called for streaming tool progress.
    context_manager: three-layer context budget control (optional).
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
    with tracer.agent_span(agent_name, session_id=session_id, task=_task):
        # Provide session-level context to hook handlers.
        if hooks:
            hooks.set_context(agent_name=agent_name, session_id=session_id)
        current_state = state
        recent_reasoning: list[str] = []
        # Accumulate token usage for the agent span
        total_prompt_tokens = 0
        total_completion_tokens = 0
        # Explore-loop guard: track tool-result nulls and repeated tool calls
        recent_null_results: list[bool] = []
        recent_tool_calls_history: list[tuple[str, str]] = []  # (name, args_key)
        # Track consecutive exploratory (read-only) tool calls to detect
        # data-exploration loops where the agent never calls a substantive tool.
        consecutive_exploratory: int = 0
        # Business artifact no-progress guard (Gap 10): track turns without
        # new non-debug artifacts being produced.
        turns_since_last_business_artifact: int = 0
        initial_business_count: int = (
            _count_business_artifacts(artifact_store) if artifact_store else 0
        )
        # Reset ToolRuntimePolicy per-run counters
        if tool_registry is not None:
            tool_registry.reset_run_state()

        while not current_state.is_terminal():
            turn_index = current_state.current_step
            timestamp = time.time()

            # Hook: pre_think
            if hooks:
                current_state = await hooks.run("pre_think", current_state)
                if current_state.is_terminal():
                    break

            # Pre-turn reminder — injected before every think phase to
            # prevent first-tool-call paralysis and mid-conversation
            # deliberation loops.  Uses role="user" because many API
            # providers reject interleaved system messages.
            current_state = current_state.model_copy(
                update={"messages": _inject_reminder(current_state.messages, PRE_TURN_REMINDER)}
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
                    tracer.log_prompt(
                        llm_span,
                        str(last_msg.get("content", "")) if isinstance(last_msg, dict) else str(last_msg),
                    )
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
                if think.state.status == "error":
                    llm_span.set_status(
                        Status(StatusCode.ERROR, "LLM call failed")
                    )
            current_state = think.state
            recent_reasoning = think.recent_reasoning

            # LLM metrics
            if think.llm_response.usage:
                record_llm_call(
                    model=think.llm_request.model or "unknown",
                    agent_name=agent_name,
                )
                usage = think.llm_response.usage
                if usage:
                    prompt = usage.get("prompt_tokens", 0)
                    completion = usage.get("completion_tokens", 0)
                    total_prompt_tokens += prompt
                    total_completion_tokens += completion
                    record_llm_tokens(
                        model=think.llm_request.model or "unknown",
                        input_tokens=prompt,
                        output_tokens=completion,
                    )

            # Model error: handle audit log and return immediately
            if current_state.status == "error":
                _maybe_write_audit_turn(audit_logger, turn_index, timestamp, think)
                if audit_logger:
                    audit_logger.finalize(
                        "error", current_state.termination_reason or "Model error"
                    )
                record_agent_request(
                    agent_name=agent_name,
                    status="error",
                )
                return current_state

            # If model returned no tool calls or reasoning loop detected, loop ends
            if current_state.is_terminal():
                _maybe_write_audit_turn(audit_logger, turn_index, timestamp, think)
                break

            # GATE: permission check
            if permissions:
                for tool_call in current_state.tool_calls:
                    if not permissions.allow(tool_call):
                        current_state = current_state.blocked(
                            f"Permission denied: {tool_call.name}"
                        )
                        break

            if current_state.is_terminal():
                _maybe_write_audit_turn(audit_logger, turn_index, timestamp, think)
                break

            # ACT + OBSERVE: execute tools, collect results
            if current_state.tool_calls and tool_registry is None:
                record_agent_request(
                    agent_name=agent_name,
                    status="error",
                )
                return current_state.errored(
                    "Tool calls requested but no tool registry configured"
                )

            if on_step:
                names = ", ".join(tc.name for tc in current_state.tool_calls)
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
            current_state = current_state.add_observation(tuple(results))

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

            # Update loop-tracking lists using the executed calls. This must
            # happen before the guard checks below so they see the current turn.
            update_null_tracking(results, recent_null_results)
            update_tool_call_history(executed_tool_calls, recent_tool_calls_history)
            consecutive_exploratory = update_exploratory_tracking(
                executed_tool_calls, consecutive_exploratory,
            )

            # Gap 10: business artifact no-progress guard — track whether
            # new business (non-debug) artifacts have been produced.
            initial_business_count, turns_since_last_business_artifact, should_terminate = \
                check_business_artifact_progress(
                    artifact_store, initial_business_count, turns_since_last_business_artifact
                )
            if should_terminate:
                current_state = current_state.model_copy(
                    update={
                        "status": "completed",
                        "termination_reason": "no_business_artifact_progress",
                    }
                )
                await _terminate_loop_step(
                    reason="no_business_artifact_progress",
                    on_step=on_step, audit_logger=audit_logger,
                    turn_index=turn_index, timestamp=timestamp,
                    think=think, tool_records=tuple(tool_records),
                )
                break

            # -- Terminal-tool readiness guard: when a terminal tool becomes ready,
            #    proactively inject a readiness summary. If the model keeps calling
            #    exploratory tools, escalate to blocking / forced termination.
            #    Blocked-tools hints: when no terminal tool is ready but some
            #    tools with contracts exist, inject a hint about what's missing.
            current_state = check_and_inject_hints(
                tool_registry=tool_registry,
                artifact_store=artifact_store,
                consecutive_exploratory=consecutive_exploratory,
                current_state=current_state,
                agent_name=agent_name,
            )
            if current_state.is_terminal():
                await _terminate_loop_step(
                    reason=current_state.termination_reason or "hints_terminated",
                    on_step=on_step, audit_logger=audit_logger,
                    turn_index=turn_index, timestamp=timestamp,
                    think=think, tool_records=tuple(tool_records),
                )
                break

            # -- Explore-loop guard: detect futile exploration patterns --
            terminated = check_explore_loop(
                current_state, recent_null_results, recent_tool_calls_history,
                consecutive_exploratory,
            )
            if terminated:
                current_state = current_state.model_copy(
                    update={
                        "status": "completed",
                        "termination_reason": "explore_loop_detected",
                    }
                )
                await _terminate_loop_step(
                    reason="explore_loop_detected",
                    on_step=on_step, audit_logger=audit_logger,
                    turn_index=turn_index, timestamp=timestamp,
                    think=think, tool_records=tuple(tool_records),
                )
                break

            # Gap 4: emit terminal_tool_called when a terminal tool was invoked
            if consecutive_exploratory == 0 and artifact_store is not None and tool_registry is not None:
                ready_tools_set = set(_get_ready_terminal_tools(tool_registry, artifact_store))
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

            if on_step:
                await on_step("observe", "results_collected")

            # Hook: post_observe
            if hooks:
                current_state = await hooks.run("post_observe", current_state)

        if audit_logger:
            audit_logger.finalize(
                final_status=current_state.status,
                termination_reason=current_state.termination_reason,
            )

        record_agent_request(
            agent_name=agent_name,
            status=current_state.status if current_state.status != "error" else "error",
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
