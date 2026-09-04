"""Guardrail base types and protocol.

Guardrails are layered checks that run at different stages of the agent loop:
- input: before the LLM call
- output: after the LLM response, before tool execution
- tool: before tool execution, turn level (a block halts the whole turn)
- tool_call: per tool call, immediately before dispatch (a deny rejects that
  single call; the run continues)
- post_tool: after tool execution

Failure contract (dual, per layer class — this is deliberate):
- Behavioral layers (input/output/tool/post_tool) are fail-open: a guard
  raising means the check is skipped and the run continues. These layers
  protect run cost/quality; hard step/budget limits bound the worst case.
- The enforcement layer (tool_call) is fail-closed: a guard raising means
  the call is denied. A permission check that fails open is a logging
  system, not a permission system.

Guards participating in the ``tool_call`` layer must implement the optional
``check_call`` method and be pure in-memory decisions (set lookups, path
resolution) — no I/O, no timeouts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol

if TYPE_CHECKING:
    from ..tool_call import ToolCall

GuardLayer = Literal["input", "output", "tool", "tool_call", "post_tool"]
GuardAction = Literal["allow", "log", "block"]
CallGuardAction = Literal["allow", "deny", "confirm"]


@dataclass(frozen=True)
class CallGuardResult:
    """Decision of a single per-call guard (``tool_call`` layer).

    ``confirm`` suspends the call for user approval via the confirmation
    chain (ConfirmationGuard → RunManager → confirmations API); without a
    confirmation handler it is denied (fail-closed).
    """

    action: CallGuardAction
    reason: str = ""
    guard_name: str = ""
    error_code: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def allow(
        cls,
        guard_name: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> "CallGuardResult":
        return cls(action="allow", guard_name=guard_name, metadata=metadata or {})

    @classmethod
    def deny(
        cls,
        guard_name: str,
        reason: str,
        error_code: str = "permission_denied",
        metadata: dict[str, Any] | None = None,
    ) -> "CallGuardResult":
        return cls(
            action="deny",
            reason=reason,
            guard_name=guard_name,
            error_code=error_code,
            metadata=metadata or {},
        )

    @classmethod
    def confirm(
        cls,
        guard_name: str,
        message: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> "CallGuardResult":
        return cls(
            action="confirm",
            reason=message,
            guard_name=guard_name,
            metadata=metadata or {},
        )


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
        # ``layer`` is enriched by GuardrailSystem at dispatch; the dataclass
        # default here is a placeholder, not a claim about the source layer.
        return cls(action="block", guard_name=guard_name, reason=reason)

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
    agent_name: str = ""
    session_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class Guardrail(Protocol):
    """A single guardrail check."""

    name: str
    layer: GuardLayer

    async def check(self, context: GuardContext) -> GuardResult: ...


class CallGuardrail(Protocol):
    """A guard participating in the ``tool_call`` (per-call) layer.

    Implementations must be pure in-memory decisions (set lookups, path
    resolution) — no I/O. Guards may implement both protocols; the per-call
    dispatch only consults objects exposing ``check_call``.
    """

    name: str

    async def check_call(self, call: ToolCall, context: GuardContext) -> CallGuardResult: ...
