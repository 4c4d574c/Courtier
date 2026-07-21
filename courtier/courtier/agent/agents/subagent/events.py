"""Unified stream event type for sub-agent activity."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class SubAgentStreamEvent:
    """Unified stream event for sub-agent activity across all layers.

    Used by AgentRuntime, SSEAdapter, and the frontend.
    """

    kind: Literal["token", "think", "tool_result", "conclusion", "start", "end"]
    subagent_name: str
    handle_id: str = ""
    parent_handle_id: str | None = None
    text: str | None = None
    detail: Any | None = None
    tool_name: str | None = None
    tool_status: str | None = None
    tool_duration: float | None = None
    tool_summary: str | None = None
    task: str | None = None
    result: Any = None
    display_name: str | None = None
    """Chinese display name for the sub-agent (e.g. "格式审核"), if configured."""
    parent_subagent_name: str | None = None
    """Name of the immediate parent sub-agent that spawned this one.

    None/empty means the sub-agent was spawned directly by the parent
    runtime context (e.g. the orchestrator for top-level sub-agents, or
    another sub-agent for nested calls). Non-empty values create a
    hierarchy so the frontend can render nested sub-agents.
    """
    scope_id: str | None = None
    """Opaque scope identifier for event isolation.

    Events emitted by a sub-agent carry the scope of their immediate
    parent. A parent runtime can choose to forward only events whose
    ``scope_id`` matches the parent's current scope, preventing
    siblings or nested runs from leaking stream events across contexts.
    """
