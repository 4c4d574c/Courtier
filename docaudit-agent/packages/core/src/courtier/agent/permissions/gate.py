"""PermissionGate — models intent must clear permission before execution."""

from __future__ import annotations

from ..core.model import ToolCall


class PermissionGate:
    """模型产生的执行意图必须先通过权限门，再变成真正动作。

    Default: allow-all. For Phase 0, no restrictions.
    """

    def __init__(self) -> None:
        self._blocked: set[str] = set()
        self._require_confirmation: dict[str, str] = {}

    def block(self, tool_name: str) -> None:
        """Block a tool entirely."""
        self._blocked.add(tool_name)

    def require_confirmation(self, tool_name: str, message: str = "") -> None:
        """Require user confirmation before executing a tool."""
        self._require_confirmation[tool_name] = message

    def needs_confirmation(self, tool_name: str) -> tuple[bool, str]:
        """Return (True, message) if tool requires confirmation, else (False, '')."""
        if tool_name in self._require_confirmation:
            return True, self._require_confirmation[tool_name]
        return False, ""

    def allow(self, tool_call: ToolCall) -> bool:
        """Check if a tool call should be allowed."""
        if tool_call.name in self._blocked:
            return False
        # require_confirmation is a soft gate — in Phase 2+ it will
        # trigger interactive confirmation. For now, allow.
        return True
