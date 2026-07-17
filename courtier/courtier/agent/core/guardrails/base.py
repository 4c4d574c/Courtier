"""Guardrail base types and protocol.

Guardrails are layered checks that run at different stages of the agent loop:
- input: before the LLM call
- output: after the LLM response, before tool execution
- tool: before/after tool execution
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

GuardLayer = Literal["input", "output", "tool", "post_tool"]
GuardAction = Literal["allow", "log", "block"]


@dataclass(frozen=True)
class GuardResult:
    """Result of a single guardrail check."""

    action: GuardAction
    reason: str = ""
    layer: GuardLayer = "input"
    guard_name: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def allow(
        cls,
        guard_name: str = "",
        reason: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> "GuardResult":
        return cls(
            action="allow",
            guard_name=guard_name,
            reason=reason,
            metadata=metadata or {},
        )

    @classmethod
    def block(cls, guard_name: str, reason: str) -> "GuardResult":
        return cls(action="block", guard_name=guard_name, reason=reason, layer="input")

    @classmethod
    def log(cls, guard_name: str, reason: str) -> "GuardResult":
        return cls(action="log", guard_name=guard_name, reason=reason)


@dataclass
class GuardContext:
    """Context passed to every guardrail check."""

    state: Any | None = None
    messages: Any | None = None
    tool_calls: Any | None = None
    tool_results: Any | None = None
    response_text: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def with_field(self, **kwargs: Any) -> "GuardContext":
        """Return a new context with updated fields."""
        return GuardContext(
            state=kwargs.get("state", self.state),
            messages=kwargs.get("messages", self.messages),
            tool_calls=kwargs.get("tool_calls", self.tool_calls),
            tool_results=kwargs.get("tool_results", self.tool_results),
            response_text=kwargs.get("response_text", self.response_text),
            metadata={**self.metadata, **kwargs.get("metadata", {})},
        )


class Guardrail(Protocol):
    """A single guardrail check."""

    name: str
    layer: GuardLayer

    async def check(self, context: GuardContext) -> GuardResult: ...
