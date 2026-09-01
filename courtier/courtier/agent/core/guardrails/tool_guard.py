"""Tool-layer guardrails."""

from __future__ import annotations

from courtier.agent.core.tool_call import ToolCall
from courtier.prompts.errors import render_error

from .base import GuardContext, GuardLayer, GuardResult


class DangerousToolGuard:
    """Block or log tool calls that look dangerous."""

    name = "dangerous_tool"
    layer: GuardLayer = "tool"

    DANGEROUS_PATTERNS = {
        "rm": "destructive shell command",
        "drop": "destructive database operation",
        "delete": "potentially destructive operation",
    }

    def __init__(self, dangerous_names: set[str] | None = None) -> None:
        self.dangerous_names = dangerous_names or set(self.DANGEROUS_PATTERNS.keys())

    async def check(self, context: GuardContext) -> GuardResult:
        tool_calls = context.tool_calls
        if tool_calls is None:
            return GuardResult.allow(self.name)

        for tc in tool_calls:
            name = tc.name if isinstance(tc, ToolCall) else tc.get("name", "")
            if name in self.dangerous_names:
                return GuardResult.block(
                    self.name,
                    render_error(
                        "errors.guard_tool_blocked",
                        tool_name=name,
                        cause=self.DANGEROUS_PATTERNS.get(name, "dangerous"),
                    ),
                )
        return GuardResult.allow(self.name)


class RepeatedToolGuard:
    """Log repeated identical tool calls within a session."""

    name = "repeated_tool"
    layer: GuardLayer = "tool"

    def __init__(self, max_repeats: int = 3) -> None:
        self.max_repeats = max_repeats
        self._history: list[tuple[str, str]] = []

    async def check(self, context: GuardContext) -> GuardResult:
        tool_calls = context.tool_calls
        if tool_calls is None:
            return GuardResult.allow(self.name)

        for tc in tool_calls:
            name = tc.name if isinstance(tc, ToolCall) else tc.get("name", "")
            args = str(tc.arguments if isinstance(tc, ToolCall) else tc.get("arguments", {}))
            key = (name, args)
            count = sum(1 for h in self._history if h == key)
            self._history.append(key)
            if count >= self.max_repeats:
                return GuardResult.log(
                    self.name,
                    f"Tool {name!r} called {count + 1} times with identical arguments",
                )
        return GuardResult.allow(self.name)
