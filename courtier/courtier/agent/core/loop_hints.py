"""Hints injection — terminal-tool readiness and blocked-tool hints.

Extracted from loop.py. Provides a single entry point check_and_inject_hints()
that checks artifact readiness and injects LLM hints to guide the model toward
(or away from) terminal tools.
"""

from __future__ import annotations

import logging
from typing import Any

from ..artifacts.models import InputField, ProjectionPolicy, build_contract_from_input_fields, derive_upstream_producers
from ..artifacts.projectors import create_default_projector_registry
from ..artifacts.resolver import ProjectionResolver, emit_event

logger = logging.getLogger(__name__)

# Max consecutive exploratory calls while a terminal tool is ready.
_MAX_EXPLORATORY_WHEN_TERMINAL_READY = 2


def _is_orchestrator(agent_name: str) -> bool:
    """Return True if *agent_name* refers to an orchestrator-type agent.

    Orchestrator agents compose Skills and should not be nudged toward
    individual plugin tools.
    """
    if not agent_name:
        return False
    return agent_name.lower().startswith("orchestrator")


def _get_effective_fields(tool: Any) -> tuple[InputField, ...] | None:
    """Return the tool's effective input_fields, completing materialize_as defaults."""
    input_fields = getattr(tool, "input_fields", None)
    if input_fields is not None and input_fields:
        return build_contract_from_input_fields(tool.name, input_fields)
    return None


def _iter_terminal_resolutions(
    tool_registry: Any | None,
    artifact_store: Any | None,
    *,
    skip_empty_candidates: bool = False,
):
    """Yield ``(tool, resolution)`` for tools with effective input fields.

    When *skip_empty_candidates* is True, tools are skipped if the artifact
    store has no projection candidates. This preserves the previous fast path
    used by readiness checks while still allowing blocked-tool hint generation
    to run with an empty candidate list.
    """
    if tool_registry is None or artifact_store is None:
        return
    tools = tool_registry.list_tools()
    registry = create_default_projector_registry()
    producers = derive_upstream_producers(tools, registry)
    for tool in tools:
        fields = _get_effective_fields(tool)
        if fields is None:
            continue
        candidates = artifact_store.list_projection_candidates()
        if skip_empty_candidates and not candidates:
            continue
        resolver = ProjectionResolver(registry)
        resolution = resolver.resolve(fields, tool.name, candidates, ProjectionPolicy(),
                                      producers=producers)
        yield tool, resolution


def _build_blocked_tools_hints(tool_registry: Any | None, artifact_store: Any | None) -> str | None:
    """Build hints for tools that are blocked due to missing artifacts.

    When a tool with require_contract_binding=True cannot resolve all fields,
    suggest the upstream tools that can produce the missing artifact types.
    """
    lines: list[str] = []
    for tool, resolution in _iter_terminal_resolutions(tool_registry, artifact_store):
        if resolution.status == "failed":
            lines.append(f"- {tool.name} 无法调用（缺少必要输入）：")
            for diagnostic in resolution.diagnostics:
                lines.append(f"  {diagnostic.message}")
            for action in resolution.suggested_actions:
                action_tool = action.get("tool", "")
                reason = action.get("reason", "")
                lines.append(f"  建议：调用 {action_tool} — {reason}")
                emit_event("tool_blocked_missing_artifact", {
                    "tool": tool.name,
                    "missing_field": action.get("field", ""),
                    "suggested_tool": action_tool,
                })
    return "\n".join(lines) if lines else None


def _is_terminal_tool_ready(tool_registry: Any | None, artifact_store: Any | None) -> bool:
    """Check whether any tool with require_contract_binding=True has all
    required fields auto-bindable from the current artifact store.

    Returns True if at least one terminal tool is ready to be called.
    """
    for _tool, resolution in _iter_terminal_resolutions(
        tool_registry, artifact_store, skip_empty_candidates=True
    ):
        if resolution.status == "resolved":
            return True
    return False


def _get_ready_terminal_tools(tool_registry: Any | None, artifact_store: Any | None) -> list[str]:
    """Return the list of terminal tool names that are ready to be called."""
    ready: list[str] = []
    for tool, resolution in _iter_terminal_resolutions(
        tool_registry, artifact_store, skip_empty_candidates=True
    ):
        if resolution.status == "resolved":
            ready.append(tool.name)
    return ready


def _build_terminal_ready_hints(tool_registry: Any | None, artifact_store: Any | None) -> str | None:
    """Build a human-readable summary of ready terminal tools and their
    auto-bound input fields.  Returns None when no tools are ready.
    """
    lines: list[str] = []
    for tool, resolution in _iter_terminal_resolutions(
        tool_registry, artifact_store, skip_empty_candidates=True
    ):
        if resolution.status == "resolved":
            # Gap 7: emit tool_ready event
            emit_event("tool_ready", {
                "tool": tool.name,
                "fields": list(resolution.plans.keys()),
            })
            lines.append(f"- {tool.name} 可以立即调用。输入将自动绑定：")
            for field_name, plan in resolution.plans.items():
                steps_desc = (
                    "（直接匹配）" if not plan.steps
                    else f"（通过 {len(plan.steps)} 步投影）"
                )
                lines.append(f"  - {field_name}{steps_desc}")
    return "\n".join(lines) if lines else None


def check_and_inject_hints(
    *,
    tool_registry: Any | None,
    artifact_store: Any | None,
    consecutive_exploratory: int,
    current_state: Any,  # AgentState
    agent_name: str = "",
) -> Any:  # Returns (possibly modified) AgentState
    """Check artifact readiness and inject hints to guide the model.

    If a terminal tool is ready:
    - On first detection: inject readiness hints so the LLM knows it can call
    - After consecutive exploratory calls exceed threshold: inject blocking hints
    - After 2x threshold: force-terminate the loop

    If no terminal tool is ready and model has been exploring:
    - Inject hints about which tools are blocked and what's missing.
    """
    # Orchestrator agents should use Skills, not call plugin tools directly.
    # Suppress terminal-tool hints so the model isn't encouraged to call
    # low-level plugin tools (e.g. detect_plagiarism) that are already
    # encapsulated by skills (e.g. plagiarism).
    if _is_orchestrator(agent_name):
        return current_state

    # -- Terminal-tool readiness guard: when a terminal tool becomes ready,
    #    proactively inject a readiness summary. If the model keeps calling
    #    exploratory tools, escalate to blocking / forced termination.
    terminal_ready = _is_terminal_tool_ready(tool_registry, artifact_store)
    if terminal_ready:
        # Gap 4: emit terminal_tool_ready event
        ready_tools = _get_ready_terminal_tools(tool_registry, artifact_store)
        emit_event("terminal_tool_ready", {
            "tools": ready_tools,
        })
        if consecutive_exploratory == 0:
            # First time terminal tool is detected as ready — inject
            # a proactive readiness hint so the LLM knows immediately.
            tool_hints = _build_terminal_ready_hints(tool_registry, artifact_store)
            if tool_hints:
                hint_msg = (
                    "\n\n[系统提示] 以下业务工具所需参数已自动准备就绪，"
                    "可直接调用，无需再通过 get_artifact 获取数据：\n"
                    + tool_hints
                )
                new_messages = list(current_state.messages)
                from .state import Message
                new_messages.append(Message(role="user", content=hint_msg))
                current_state = current_state.model_copy(
                    update={"messages": tuple(new_messages)}
                )
                logger.debug("Injected terminal-tool readiness hints.")
        elif consecutive_exploratory >= _MAX_EXPLORATORY_WHEN_TERMINAL_READY:
            logger.warning(
                "Terminal tool is ready but model called %d consecutive "
                "exploratory tools. Blocking further exploration.",
                consecutive_exploratory,
            )
            hint_msg = (
                "\n\n[系统提示] 业务工具（如 detect_plagiarism）所需的参数"
                "已自动准备就绪。请直接调用目标业务工具，"
                "无需再调用 get_artifact 或 list_artifacts。"
            )
            new_messages = list(current_state.messages)
            from .state import Message
            new_messages.append(Message(role="user", content=hint_msg))
            current_state = current_state.model_copy(
                update={"messages": tuple(new_messages)}
            )
            # After 2x the threshold with no change, force termination
            if consecutive_exploratory >= _MAX_EXPLORATORY_WHEN_TERMINAL_READY * 2:
                current_state = current_state.model_copy(
                    update={
                        "status": "completed",
                        "termination_reason": "terminal_tool_ready_but_ignored",
                    }
                )

    # -- Blocked-tools hints: when no terminal tool is ready but some
    #    tools with contracts exist, inject a hint about what's missing.
    if not terminal_ready and consecutive_exploratory >= 2:
        blocked_hints = _build_blocked_tools_hints(tool_registry, artifact_store)
        if blocked_hints:
            hint_msg = (
                "\n\n[系统提示] 以下业务工具因缺少必要输入暂无法调用：\n"
                + blocked_hints
                + "\n请先调用建议的上游工具获取所需数据。"
            )
            new_messages = list(current_state.messages)
            from .state import Message
            new_messages.append(Message(role="user", content=hint_msg))
            current_state = current_state.model_copy(
                update={"messages": tuple(new_messages)}
            )
            logger.debug("Injected blocked-tools hints.")

    return current_state
