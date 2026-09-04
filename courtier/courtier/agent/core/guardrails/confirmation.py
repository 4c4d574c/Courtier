"""ConfirmationGuard — tools requiring explicit user approval before dispatch.

One of the ``tool_call`` layer guards. Returns ``confirm`` for tools on the
configured list; the loop-side handler (wired by RunManager) suspends the
call until the user resolves it via the confirmations API. Session-approved
tools (``approve_session``) short-circuit to allow — the approved set is
shared with the API layer so a mid-run decision takes effect immediately;
cross-restart persistence lives in ``SessionRecord.approved_tools``.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from .base import CallGuardResult, GuardLayer

if TYPE_CHECKING:
    from ..tool_call import ToolCall
    from .registry import GuardSessionContext


class ConfirmationGuard:
    """Per-call guard: listed tools return a ``confirm`` decision."""

    name = "tool_confirmation"
    layer: GuardLayer = "tool_call"

    def __init__(
        self,
        rules: Iterable[Any] = (),
        approved_tools: set[str] | None = None,
    ) -> None:
        # rules: objects with .tool / .message (Settings.tool_confirmation).
        self._rules: dict[str, str] = {rule.tool: (rule.message or "") for rule in rules or []}
        self.approved_tools = approved_tools if approved_tools is not None else set()

    @classmethod
    def build(cls, ctx: "GuardSessionContext") -> "ConfirmationGuard":
        """Declarative entry: rules come from settings.tool_confirmation;
        ``approved_tools`` must be the live session set so mid-run approvals
        land here without a rebuild."""
        return cls(
            rules=getattr(ctx.settings, "tool_confirmation", None) or (),
            approved_tools=ctx.approved_tools,
        )

    def rule_for(self, tool_name: str) -> str | None:
        """Return the confirmation message for *tool_name*, if listed."""
        if tool_name in self.approved_tools:
            return None
        return self._rules.get(tool_name)

    def approve_session(self, tool_name: str) -> None:
        """Approve *tool_name* for the rest of the session (live set)."""
        self.approved_tools.add(tool_name)

    async def check_call(self, call: "ToolCall", context: Any) -> CallGuardResult:
        message = self.rule_for(call.name)
        if message is None:
            return CallGuardResult.allow(self.name)
        return CallGuardResult.confirm(
            self.name,
            message or f"工具 {call.name} 需要确认后执行",
            metadata={"tool": call.name},
        )
