"""PermissionGate — models intent must clear permission before execution."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from ..core.model import ToolCall

#: Tools whose ``path`` argument is validated against the allowed roots.
#: The gate carries no knowledge of specific tools beyond this list —
#: the file tools themselves are policy-free primitives.
DEFAULT_PATH_POLICY_TOOLS = ("read", "edit", "write")


class PermissionGate:
    """模型产生的执行意图必须先通过权限门，再变成真正动作。

    两级检查，统一单调用拒绝语义（``check`` 返回拒绝原因，调用方据以
    合成错误结果并继续运行，绝不整轮熔断）：

    - 名级：工具被整体禁用（``block``）→ "工具 X 已被禁用"；
    - 参数级：``read``/`edit``/``write`` 的 ``path`` 经 ``resolve()``
      归一（消化 ``..`` 与 symlink）后必须落在允许根内 → 带根清单的
      拒绝原因。

    ``allowed_roots`` 为空/None 时参数级策略不激活（默认 allow-all，
    保持既有行为）；``build_agent`` 按会话构造门时注入会话专属根。
    路径解析异常按 fail-closed 处理：拒绝。
    """

    def __init__(
        self,
        *,
        allowed_roots: Sequence[str | Path] | None = None,
        path_policy_tools: Sequence[str] = DEFAULT_PATH_POLICY_TOOLS,
    ) -> None:
        self._blocked: set[str] = set()
        self._require_confirmation: dict[str, str] = {}
        self._path_tools = frozenset(path_policy_tools)
        self._roots = (
            [Path(root).expanduser().resolve() for root in allowed_roots]
            if allowed_roots
            else []
        )

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

    def check(self, tool_call: ToolCall) -> str | None:
        """Return a denial reason for this call, or None when allowed.

        The reason is surfaced to the model as the call's error result —
        single-call rejection: the run continues and the model can adapt.
        """
        name = tool_call.name
        if name in self._blocked:
            return f"工具 {name} 已被禁用，请改用其它工具或直接给出文本回答"
        if self._roots and name in self._path_tools:
            return self._check_path(name, (tool_call.arguments or {}).get("path"))
        return None

    def allow(self, tool_call: ToolCall) -> bool:
        """Boolean form of :meth:`check` (legacy loop seam until it migrates)."""
        return self.check(tool_call) is None

    def _check_path(self, tool_name: str, path: object) -> str | None:
        if path is None:
            # Absent path → the tool's own "必须提供 path" error is clearer.
            return None
        if not isinstance(path, str) or not path:
            return f"{tool_name} 的 path 参数必须为非空字符串"
        try:
            resolved = Path(path).expanduser().resolve()
        except Exception:
            return f"路径无法解析，已拒绝: {path!r}"
        for root in self._roots:
            if resolved.is_relative_to(root):
                return None
        roots_listing = "\n".join(f"- {root}" for root in self._roots)
        return (
            f"路径 {path} 不在允许范围内。允许的根目录：\n{roots_listing}\n"
            f"请把路径限制在以上目录内。"
        )
