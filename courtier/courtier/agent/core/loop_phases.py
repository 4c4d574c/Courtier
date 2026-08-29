"""Extracted phases from agent_loop — think and tool-execute."""

from __future__ import annotations

import inspect
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from courtier.agent.core.execution_result import ExecutionResult

from .audit_logger import LLMRequestRecord, LLMResponseRecord
from .loop_guards import detect_reasoning_loop
from .loop_streaming import generate_with_streaming_fallback

if TYPE_CHECKING:
    from ..tools.registry import ToolRegistry
    from .model import ModelClient
    from .state import AgentState

logger = logging.getLogger(__name__)


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

    try:
        if on_token is not None:
            # Notify frontend before streaming starts so it can create a
            # placeholder step for real-time thought rendering.
            if on_step:
                await on_step("think", "text_response")
            response, tokens_streamed = await generate_with_streaming_fallback(
                model=model,
                messages=messages,
                tools=tools_schemas,
                on_token=on_token,
                on_content_token=on_content_token,
            )
            if tokens_streamed and on_step:
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
        state = state.errored(f"Model error: {exc}", set_status=False)
        return ThinkResult(
            state=state,
            llm_request=llm_request,
            llm_response=error_response,
            llm_duration_ms=llm_duration_ms,
            recent_reasoning=recent_reasoning,
            tokens_streamed=False,
            failed=True,
        )

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
        usage=response.usage,
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
) -> tuple[list[ExecutionResult], list[Any]]:
    """Execute all tool calls in the current state.

    Returns: (results: list[ExecutionResult], records: list[ToolExecutionRecord])
    """
    from .audit_logger import ToolExecutionRecord
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
                error=(
                    f"Failed to parse arguments for tool '{tool_call.name}'. "
                    f"Raw arguments: {raw_args}"
                ),
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
            result = ExecutionResult.from_error(
                actor_type="tool",
                actor_name=tool_call.name,
                error=str(exc),
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
