"""Permission guards for the ``tool_call`` layer.

Name-level rule (``ToolDisabledGuard``) and path whitelist
(``PathPolicyGuard``). The path policy's per-tool scope is **declaration
is the whole truth**: a tool that carries
``path_policy = {"path": [p1, p2, ...]}`` is governed by exactly those
roots — nothing else applies, and there is no implicit default.
``False`` or no attribute means the tool is not governed by the path
policy at all. Built-in file tools (read/edit/write) are the platform
baseline: governed by the session roots via the fallback list.

Denial texts are byte-identical to the former ``PermissionGate`` and are
part of the regression contract (see
docs/architecture/guardrails-unification-plan.md).
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .base import CallGuardResult, GuardLayer

if TYPE_CHECKING:
    from ..tool_call import ToolCall

#: 兜底名单：未做 Capability 声明的工具里，这些名字仍受会话路径白名单管。
#: 内置文件工具（read/edit/write）的会话根约束由它承担——平台基线。
DEFAULT_PATH_POLICY_TOOLS = ("read", "edit", "write")


def declare_path_policy_tools(registry: Any, tools: Iterable[Any]) -> list[str]:
    """把工具自声明的 path_policy 注册为 Capability 名片。

    声明即全部：``{"path": [p1, p2, ...]}`` → 该工具恰好只能读写这些路径
    （支持 ``~`` 展开）。``False`` / 未声明 → 不受管。声明不完整直接抛
    ValueError——安全声明宁可 fail loud 也不静默裸奔。返回已声明的工具名。
    """
    from courtier.agent.core.capability import Capability

    declared: list[str] = []
    for tool in tools:
        name = getattr(tool, "name", None)
        value = getattr(tool, "path_policy", None)
        if not name or value is None or value is False:
            continue
        if not (isinstance(value, dict) and "path" in value):
            raise ValueError(
                f"工具 {name} 的 path_policy 声明必须是 "
                f'{{"path": [路径, ...]}}: {value!r}'
            )
        paths = [os.path.expanduser(str(p)) for p in value["path"]]
        registry.register(
            Capability(
                type="tool",
                name=name,
                meta={"permission": {"path_policy": {"path": paths}}},
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
    """Parameter-level rule: file-tool paths must stay inside declared roots.

    每个工具的生效根，按顺序取第一处能回答的来源：

    1. Capability 声明（``meta["permission"]["path_policy"]``）：
       ``{"path": [...]}`` → 恰好这些根；声明不完整 → fail-closed 空根
       （拒绝一切路径）；``False`` → 显式豁免，不受管。
    2. 未声明 → 老名单（``read``/`edit``/``write``）受**会话根**管；
       其余工具不受管。

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

    def _managed_roots(self, tool_name: str) -> list[str] | None:
        """该工具生效的根列表；None = 不受管（空列表 = fail-closed 拒绝一切）。"""
        if self._capabilities is not None:
            capability = self._capabilities.get("tool", tool_name)
            if capability is not None:
                permission = getattr(capability, "meta", {}).get("permission")
                if isinstance(permission, dict):
                    declaration = permission.get("path_policy")
                    if declaration is False:
                        return None                 # 显式豁免 → 不受管
                    if isinstance(declaration, dict) and "path" in declaration:
                        return [
                            os.path.expanduser(str(p))
                            for p in declaration["path"]
                        ]
                    return []                       # 声明不完整 → fail-closed
        if tool_name in self._path_tools:
            return [str(root) for root in self._roots]
        return None

    async def check_call(self, call: "ToolCall", context: Any) -> CallGuardResult:
        roots = self._managed_roots(call.name)
        # roots is None = 未受管 → 放行；roots == [] = fail-closed → 全拒。
        if not self._roots or roots is None:
            return CallGuardResult.allow(self.name)
        denial = self._check_path(
            call.name, (call.arguments or {}).get("path"), roots
        )
        if denial is None:
            return CallGuardResult.allow(self.name)
        return CallGuardResult.deny(self.name, denial)

    def _check_path(
        self, tool_name: str, path: object, roots: list[str]
    ) -> str | None:
        if path is None:
            # Absent path → the tool's own "必须提供 path" error is clearer.
            return None
        if not isinstance(path, str) or not path:
            return f"{tool_name} 的 path 参数必须为非空字符串"
        try:
            resolved = Path(path).expanduser().resolve()
        except Exception:
            return f"路径无法解析，已拒绝: {path!r}"
        resolved_roots = [Path(root).expanduser().resolve() for root in roots]
        for root in resolved_roots:
            if resolved.is_relative_to(root):
                return None
        roots_listing = "\n".join(f"- {root}" for root in resolved_roots)
        return (
            f"路径 {path} 不在允许范围内。允许的根目录：\n{roots_listing}\n"
            f"请把路径限制在以上目录内。"
        )
