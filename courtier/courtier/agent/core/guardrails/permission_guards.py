"""Permission guards for the ``tool_call`` layer.

Ported verbatim from ``PermissionGate`` (deleted in the guardrails
unification) — the denial texts are byte-identical and are part of the
regression contract (see docs/architecture/guardrails-unification-plan.md).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .base import CallGuardResult, GuardLayer

if TYPE_CHECKING:
    from ..tool_call import ToolCall

#: Tools whose ``path`` argument is validated against the allowed roots.
#: The guard carries no knowledge of specific tools beyond this list —
#: the file tools themselves are policy-free primitives. (From T7 on, the
#: list is overridden per tool by ``Capability.meta["permission"]``.)
DEFAULT_PATH_POLICY_TOOLS = ("read", "edit", "write")


def declare_path_policy_tools(registry: Any, tools: Iterable[Any]) -> list[str]:
    """Register path-policy declarations for tools that self-declare.

    A tool opts in with a truthy ``path_policy`` class attribute — the
    declaration lands in the session ``CapabilityRegistry`` as data, so
    adding a governed tool never means editing the wiring code. Explicit
    declarations beat ``PathPolicyGuard``'s legacy fallback list. Returns
    the declared tool names.
    """
    from courtier.agent.core.capability import Capability

    declared: list[str] = []
    for tool in tools:
        name = getattr(tool, "name", None)
        if not name or not getattr(tool, "path_policy", False):
            continue
        registry.register(
            Capability(
                type="tool",
                name=name,
                meta={"permission": {"path_policy": True}},
            )
        )
        declared.append(name)
    return declared


class ToolDisabledGuard:
    """Name-level rule: a tool on the block list is denied entirely."""

    name = "tool_disabled"
    layer: GuardLayer = "tool_call"

    def __init__(self, blocked: Sequence[str] = ()) -> None:
        self._blocked = frozenset(blocked)

    async def check_call(self, call: "ToolCall", context: Any) -> CallGuardResult:
        if call.name in self._blocked:
            return CallGuardResult.deny(
                self.name,
                f"工具 {call.name} 已被禁用，请改用其它工具或直接给出文本回答",
            )
        return CallGuardResult.allow(self.name)


class PathPolicyGuard:
    """Parameter-level rule: file-tool paths must resolve inside the roots.

    Which tools the policy applies to: an explicit capability declaration
    (``Capability.meta["permission"]["path_policy"]``) wins when present —
    including an explicit ``False`` opt-out for a default-list tool;
    otherwise the tool must be on ``path_policy_tools`` (the legacy
    read/edit/write list).

    Inactive when no roots are configured (allow-all, preserving legacy
    behavior). Path resolution exceptions are fail-closed: deny.
    """

    name = "path_policy"
    layer: GuardLayer = "tool_call"

    def __init__(
        self,
        allowed_roots: Sequence[str | Path] | None = None,
        path_policy_tools: Sequence[str] = DEFAULT_PATH_POLICY_TOOLS,
        capability_registry: Any | None = None,
    ) -> None:
        self._path_tools = frozenset(path_policy_tools)
        self._capabilities = capability_registry
        self._roots = (
            [Path(root).expanduser().resolve() for root in allowed_roots]
            if allowed_roots
            else []
        )

    def _policy_applies(self, tool_name: str) -> bool:
        if self._capabilities is not None:
            capability = self._capabilities.get("tool", tool_name)
            if capability is not None and isinstance(
                capability.meta.get("permission"), dict
            ):
                return bool(capability.meta["permission"].get("path_policy", False))
        return tool_name in self._path_tools

    async def check_call(self, call: "ToolCall", context: Any) -> CallGuardResult:
        if not self._roots or not self._policy_applies(call.name):
            return CallGuardResult.allow(self.name)
        denial = self._check_path(call.name, (call.arguments or {}).get("path"))
        if denial is None:
            return CallGuardResult.allow(self.name)
        return CallGuardResult.deny(self.name, denial)

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
