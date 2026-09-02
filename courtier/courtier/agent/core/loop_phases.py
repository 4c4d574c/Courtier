"""Extracted phases from agent_loop — think and tool-execute."""

from __future__ import annotations

import inspect
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from courtier.agent.core.execution_result import ExecutionResult
from courtier.prompts.errors import render_error

from .audit_logger import LLMRequestRecord, LLMResponseRecord
from .loop_guards import detect_reasoning_loop
from .loop_streaming import generate_with_streaming_fallback

if TYPE_CHECKING:
    from ..tools.registry import ToolRegistry
    from .model import ModelClient
    from .state import AgentState

logger = logging.getLogger(__name__)

# Cap for raw exception text surfaced to the model — a runaway traceback in
# an exception message must not flood the observation (the full traceback is
# still in the server log via logger.exception).
_MAX_EXCEPTION_CHARS = 800


@dataclass
class ThinkResult:
    """Output of the think phase."""

    state: "AgentState"
    llm_request: LLMRequestRecord
    llm_response: LLMResponseRecord
    llm_duration_ms: int
    recent_reasoning: list[str]
    tokens_streamed: bool
    failed: bool = False
    reasoning_loop: bool = False
    #: Same-model retries performed after refusal detection (final response
    #: is what lands in state; refused attempts are discarded).
    refusal_attempts: int = 0
    #: Set when the final attempt still matched a refusal pattern after the
    #: retry budget was spent (retry_max > 0) — the caller surfaces a notice.
    refusal_exhausted: str | None = None


async def think_phase(
    *,
    state: "AgentState",
    model: "ModelClient",
    tool_registry: "ToolRegistry | None",
    context_manager: Any,
    recent_reasoning: list[str],
    on_step: Any,
    on_token: Any,
    on_content_token: Any,
    publish: Any | None = None,
) -> ThinkResult:
    """Think phase: compact context, call model, detect reasoning loops.

    Returns a ThinkResult. The caller checks whether the state became
    terminal (model returned no tool calls, or reasoning loop detected,
    or model generation failed).
    """
    turn_index = state.current_step

    # --- Context budget: Layer 3 full compaction before think ---
    if context_manager:
        before_messages = state.messages
        compacted = await context_manager.compact_if_needed(
            before_messages,
            # Fires only when the budget is actually exceeded, before the
            # slow LLM summarization — lets the frontend show "compacting".
            on_compact_start=((lambda: on_step("compacting", "")) if on_step is not None else None),
        )
        if len(compacted) != len(before_messages):
            # Notify the frontend that context was compacted (the summary
            # silently replaced history — users should know).
            if on_step is not None:
                detail = f"{len(before_messages)} 条消息 → {len(compacted)} 条"
                if getattr(
                    getattr(context_manager, "state", None),
                    "last_compact_over_budget",
                    False,
                ):
                    detail += "（压缩后仍超预算）"
                await on_step("compact", detail)
        state = state.model_copy(update={"messages": compacted})
        if state.is_terminal():
            return ThinkResult(
                state=state,
                llm_request=LLMRequestRecord(messages=[], tools=None, model="", temperature=None),
                llm_response=LLMResponseRecord(
                    content=None,
                    reasoning=None,
                    tool_calls=[],
                    usage=None,
                    finish_reason="error",
                    duration_ms=0,
                ),
                llm_duration_ms=0,
                recent_reasoning=recent_reasoning,
                tokens_streamed=False,
            )

    # --- Memory recall: once per real user turn (MemoryManager only) ---
    # iscoroutinefunction keeps MagicMock-based test doubles (which produce
    # a callable attribute for ANY name) from being "awaited".
    inject_memory = getattr(context_manager, "inject_memory_recall", None)
    if inject_memory is not None and inspect.iscoroutinefunction(inject_memory):
        injected = await inject_memory(state.messages)
        if injected != state.messages:
            state = state.model_copy(update={"messages": injected})

    # THINK: model generates next action
    messages = state.to_openai_messages()
    tools_schemas = (
        tool_registry.get_schemas(hide_debug_for_task_agents=True) if tool_registry else None
    )
    model_name = getattr(model, "model_name", "unknown")
    temperature = getattr(model, "temperature", None)

    llm_request = LLMRequestRecord(
        messages=list(messages),
        tools=tools_schemas,
        model=model_name,
        temperature=temperature,
    )

    llm_start = time.perf_counter()
    tokens_streamed = False

    # Refusal retry policy (model-behavior recovery, outside guardrails):
    # a pure-text response matching a refusal pattern is discarded and the
    # same model re-asked unchanged, up to ``refusal_retry_max`` times.
    from courtier.config import get_settings

    from .refusal import RefusalDetector
    from ..telemetry.metrics import record_refusal

    _settings = get_settings()
    detector = (
        RefusalDetector(_settings.refusal_patterns)
        if _settings.refusal_detection_enabled
        else None
    )
    max_refusal_retries = (
        max(0, int(_settings.refusal_retry_max)) if detector is not None else 0
    )
    refusal_attempt = 0
    refusal_exhausted: str | None = None
    usage_totals: dict[str, int] = {}

    while True:
        try:
            if on_token is not None:
                # Notify frontend before streaming starts so it can create a
                # placeholder step for real-time thought rendering (first
                # attempt only — retries reset the buffers via think.retry).
                if on_step and refusal_attempt == 0:
                    await on_step("think", "text_response")
                response, tokens_streamed = await generate_with_streaming_fallback(
                    model=model,
                    messages=messages,
                    tools=tools_schemas,
                    on_token=on_token,
                    on_content_token=on_content_token,
                )
                if tokens_streamed and on_step and refusal_attempt == 0:
                    await on_step("stream", "streaming_response")
            else:
                response = await model.generate(messages, tools=tools_schemas)
        except Exception as exc:
            logger.exception("Model generation failed")
            llm_duration_ms = int((time.perf_counter() - llm_start) * 1000)
            error_response = LLMResponseRecord(
                content=None,
                reasoning=None,
                tool_calls=[],
                usage=None,
                finish_reason="error",
                duration_ms=llm_duration_ms,
            )
            state = state.errored(render_error("errors.model_error", error=str(exc)), set_status=False)
            return ThinkResult(
                state=state,
                llm_request=llm_request,
                llm_response=error_response,
                llm_duration_ms=llm_duration_ms,
                recent_reasoning=recent_reasoning,
                tokens_streamed=False,
                failed=True,
                refusal_attempts=refusal_attempt,
            )

        # Usage accumulates across refused attempts — the tokens were spent.
        if response.usage:
            for key in ("prompt_tokens", "completion_tokens"):
                usage_totals[key] = usage_totals.get(key, 0) + int(
                    response.usage.get(key, 0) or 0
                )

        refusal_matched = None
        if detector is not None and not response.tool_calls:
            refusal_matched = detector.match(response.content)
        if refusal_matched is not None:
            record_refusal("detected")
            if publish is not None:
                await publish(
                    "refusal.detected",
                    {
                        "matched": refusal_matched[:40],
                        "attempt": refusal_attempt,
                    },
                )
            if refusal_attempt < max_refusal_retries:
                refusal_attempt += 1
                logger.warning(
                    "Refusal detected (attempt %d matched %r) — retrying the "
                    "same model unchanged (%d/%d)",
                    refusal_attempt,
                    refusal_matched[:40],
                    refusal_attempt,
                    max_refusal_retries,
                )
                if publish is not None:
                    await publish("think.retry", {"attempt": refusal_attempt})
                if on_step:
                    await on_step("think_retry", str(refusal_attempt))
                llm_start = time.perf_counter()
                tokens_streamed = False
                continue
            if max_refusal_retries > 0:
                # Retry budget spent and the final answer still refuses —
                # surface it; the response itself still lands in the flow.
                refusal_exhausted = refusal_matched
                record_refusal("exhausted")
                if publish is not None:
                    await publish(
                        "refusal.exhausted",
                        {
                            "matched": refusal_matched[:40],
                            "text": render_error("errors.refusal_exhausted"),
                        },
                    )

        break

    llm_duration_ms = int((time.perf_counter() - llm_start) * 1000)

    # Calibrate the context budget with the provider-reported prompt tokens
    # so the next compaction check never triggers late (heuristic can
    # underestimate, especially for mixed CJK/ASCII content).
    update_usage = getattr(context_manager, "update_actual_usage", None)
    if update_usage is not None and response.usage:
        update_usage(response.usage.get("prompt_tokens"))

    llm_response = LLMResponseRecord(
        content=response.content,
        reasoning=response.reasoning_content,
        tool_calls=[
            {"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in response.tool_calls
        ],
        # Refused attempts' tokens were spent too — surface the sum.
        usage=usage_totals or response.usage,
        finish_reason=response.finish_reason,
        duration_ms=llm_duration_ms,
    )

    # Log reasoning/thinking content to structured events log so the
    # streaming thinking process is captured in structured_events.jsonl.
    if response.reasoning_content:
        logger.debug(
            "Turn %d reasoning (%d chars): %s",
            turn_index,
            len(response.reasoning_content),
            response.reasoning_content[:600]
            + ("..." if len(response.reasoning_content) > 600 else ""),
        )

    # Token usage is published as a structured ``llm.usage`` event by
    # ``_run_think_phase`` (loop.py); the legacy "prompt,completion" string
    # protocol was removed with the event-bus migration.

    if on_step:
        if response.tool_calls:
            names = ", ".join(tc.name for tc in response.tool_calls)
            await on_step("think", f"tool_calls: {names}")
        elif not tokens_streamed:
            await on_step("think", "text_response")

    state = state.add_thought(response, set_status=False)

    # Detect reasoning loop: if model's reasoning is near-identical across
    # consecutive steps, it's stuck — force completion to stop wasting tokens.
    reasoning_loop = False
    if response.reasoning_content:
        recent_reasoning.append(response.reasoning_content)
        if detect_reasoning_loop(recent_reasoning):
            logger.warning(
                "Reasoning loop detected (%d consecutive near-duplicate steps). "
                "Forcing completion.",
                3,
            )
            reasoning_loop = True

    return ThinkResult(
        state=state,
        llm_request=llm_request,
        llm_response=llm_response,
        llm_duration_ms=llm_duration_ms,
        recent_reasoning=recent_reasoning,
        tokens_streamed=tokens_streamed,
        reasoning_loop=reasoning_loop,
        refusal_attempts=refusal_attempt,
        refusal_exhausted=refusal_exhausted,
    )


def _wrap_tool_start(
    callback: Callable[..., Awaitable[None]] | None, call_id: str
) -> Callable[[str], Awaitable[None]] | None:
    """Attach *call_id* to a registry-level on_tool_start(name) callback.

    The registry only knows the tool name; the loop knows the specific
    call. Same-name parallel calls are paired to their UI cards and
    citations by tool_call_id, so the id must ride along on every event.
    """
    if callback is None:
        return None

    async def wrapped(name: str) -> None:
        await callback(name, call_id)

    return wrapped


async def execute_tools_phase(
    *,
    state: "AgentState",
    tool_registry: "ToolRegistry | None",
    context_manager: Any,
    artifact_store: Any,
    on_tool_result: Callable[..., Awaitable[None]] | None,
    on_tool_start: Callable[..., Awaitable[None]] | None = None,
    on_tool_progress: Callable[[str, Any], Awaitable[None]] | None = None,
    audit_logger: Any | None = None,
    guardrail_system: Any | None = None,
    confirmation_handler: Any | None = None,
) -> tuple[list[ExecutionResult], list[Any]]:
    """Execute all tool calls in the current state.

    Permission guards (tool_call layer): a denied call becomes that call's
    error result (single-call rejection — the run continues) and never
    dispatches.

    Returns: (results: list[ExecutionResult], records: list[ToolExecutionRecord])
    """
    from .audit_logger import ToolExecutionRecord
    from .guardrails.base import GuardContext
    from .loop_utils import tool_result_summary

    results: list[ExecutionResult] = []
    records: list[ToolExecutionRecord] = []

    if tool_registry is None:
        # agent_loop transitions to error when calls are pending without a
        # registry, so this is only reachable with an empty call list.
        return results, records

    for tool_call in state.tool_calls:
        tool_start = time.perf_counter()

        # Guard: if argument parsing failed, surface it immediately
        if "_parse_error" in tool_call.arguments:
            raw_args = tool_call.arguments.get("raw", "")[:200]
            result = ExecutionResult.from_error(
                actor_type="tool",
                actor_name=tool_call.name,
                error=render_error(
                    "errors.tool_arg_parse",
                    tool_name=tool_call.name,
                    raw_arguments=raw_args,
                ),
                metadata={"error_code": "tool_arg_parse"},
            )
            tool_duration_ms = int((time.perf_counter() - tool_start) * 1000)
            records.append(
                ToolExecutionRecord(
                    tool_name=tool_call.name,
                    tool_call_id=tool_call.id,
                    arguments=dict(tool_call.arguments),
                    result_success=result.success,
                    result_data=result.raw_data,
                    result_error=result.error,
                    duration_ms=tool_duration_ms,
                )
            )
            results.append(result)
            continue

        # Permission guards — single-call rejection: a denied call becomes its
        # own error result (reason + guidance for the model) and is never
        # dispatched; the run continues with the remaining calls.
        if guardrail_system is not None:
            decision = await guardrail_system.check_call(
                "tool_call",
                tool_call,
                GuardContext(state=state),
            )
            if decision.action == "deny":
                result = ExecutionResult.from_error(
                    actor_type="tool",
                    actor_name=tool_call.name,
                    error=decision.reason,
                    metadata={"error_code": decision.error_code or "permission_denied"},
                )
                tool_duration_ms = int((time.perf_counter() - tool_start) * 1000)
                if on_tool_result:
                    summary = tool_result_summary(result)
                    await on_tool_result(tool_call.name, result, summary, tool_call.id)
                records.append(
                    ToolExecutionRecord(
                        tool_name=tool_call.name,
                        tool_call_id=tool_call.id,
                        arguments=dict(tool_call.arguments),
                        result_success=result.success,
                        result_data=result.raw_data,
                        result_error=result.error,
                        duration_ms=tool_duration_ms,
                    )
                )
                results.append(result)
                continue
            elif decision.action == "confirm":
                # Confirmation chain: the handler (wired by RunManager)
                # suspends until the user resolves; without a handler the
                # call fails closed. Approval falls through to dispatch.
                approved = False
                if confirmation_handler is not None:
                    approved = await confirmation_handler(tool_call, decision.reason)
                if not approved:
                    denial_reason = (
                        render_error(
                            "errors.confirmation_unavailable",
                            tool_name=tool_call.name,
                        )
                        if confirmation_handler is None
                        else render_error(
                            "errors.confirmation_denied",
                            tool_name=tool_call.name,
                            message=decision.reason,
                        )
                    )
                    result = ExecutionResult.from_error(
                        actor_type="tool",
                        actor_name=tool_call.name,
                        error=denial_reason,
                        metadata={"error_code": "confirmation_denied"},
                    )
                    tool_duration_ms = int((time.perf_counter() - tool_start) * 1000)
                    if on_tool_result:
                        summary = tool_result_summary(result)
                        await on_tool_result(tool_call.name, result, summary, tool_call.id)
                    records.append(
                        ToolExecutionRecord(
                            tool_name=tool_call.name,
                            tool_call_id=tool_call.id,
                            arguments=dict(tool_call.arguments),
                            result_success=result.success,
                            result_data=result.raw_data,
                            result_error=result.error,
                            duration_ms=tool_duration_ms,
                        )
                    )
                    results.append(result)
                    continue

        try:
            # ArtifactStore now subsumes CacheStore functionality
            # (persist, resolve_refs).  Pass only artifact_store.
            result = await tool_registry.execute(
                tool_call.name,
                context_manager=context_manager,
                artifact_store=artifact_store,
                on_tool_start=_wrap_tool_start(on_tool_start, tool_call.id),
                on_tool_progress=on_tool_progress,
                audit_logger=audit_logger,
                **tool_call.arguments,
            )
        except Exception as exc:
            logger.exception("Tool %s failed", tool_call.name)
            exc_text = str(exc)
            if len(exc_text) > _MAX_EXCEPTION_CHARS:
                exc_text = exc_text[:_MAX_EXCEPTION_CHARS] + "…"
            result = ExecutionResult.from_error(
                actor_type="tool",
                actor_name=tool_call.name,
                error=render_error(
                    "errors.tool_exception",
                    tool_name=tool_call.name,
                    error=exc_text,
                ),
                metadata={"error_code": "tool_exception"},
            )

        tool_duration_ms = int((time.perf_counter() - tool_start) * 1000)

        # Notify display of tool result
        if on_tool_result:
            summary = tool_result_summary(result)
            await on_tool_result(tool_call.name, result, summary, tool_call.id)

        records.append(
            ToolExecutionRecord(
                tool_name=tool_call.name,
                tool_call_id=tool_call.id,
                arguments=dict(tool_call.arguments),
                result_success=result.success,
                result_data=result.raw_data if result.raw_data is not None else result.summary,
                result_error=result.error,
                duration_ms=tool_duration_ms,
            )
        )
        results.append(result)

    return results, records
