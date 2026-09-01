"""Unified event model for the agent runtime.

All significant state changes inside ``agent_loop`` are published as
``AgentEvent`` instances. Consumers such as the SSE adapter, audit logger,
and debugging tools subscribe to an ``EventBus`` and react independently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

EventType = Literal[
    "state.transition",
    "llm.request",
    "llm.token",
    "llm.content_token",
    "llm.response",
    "llm.usage",
    "think.tool_calls",
    "think.text_response",
    "tool.start",
    "tool.progress",
    "tool.result",
    "tool.error",
    "guard.triggered",
    "hint.injected",
    "loop.completed",
    "subagent.event",
    "model.selected",
    "model.fallback",
    "context.compacting",
    "context.compacted",
]


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """A single, immutable event emitted by the agent runtime.

    Attributes:
        type: The event category.
        session_id: Logical session identifier.
        agent_name: Name of the agent that emitted the event.
        turn_index: Zero-based turn index within the run.
        payload: Event-specific data. Payload shapes are defined by the
            producers (see ``loop.py`` / ``loop_hints.py`` call sites); they
            are not yet formally schema-versioned.
        event_id: Globally unique event identifier.
        timestamp: UTC timestamp when the event was created.
    """

    type: EventType
    session_id: str
    agent_name: str
    turn_index: int
    payload: dict[str, Any]
    event_id: str = field(default_factory=lambda: uuid4().hex)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
