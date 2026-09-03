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


def resolve_path_policy_declaration(
    value: Any, *, memory_home: Any, workspace: Any
) -> list[str] | None:
    """把工具自声明的 path_policy 值解析为具体根列表。

    声明即全部：写明哪些路径，工具就恰好只有那些路径，没有任何隐式默认。

    - "session" → 会话自有空间（memory_home + workspace）——内置文件工具用
    - {"subpath": p} → 仅 workspace/p（收窄：工具只能写自己的子目录）
    - {"roots": [...]} → 恰好这些根（路径展开 ~，相对路径相对 workspace）
    - False → None（豁免，不注册名片）

    非法声明抛 ValueError——安全声明宁可 fail loud 也不静默裸奔。
    """
    if value is False:
        return None
    if value == "session":
        return [str(memory_home), str(workspace)]
    if isinstance(value, dict):
        keys = set(value)
        if keys == {"subpath"}:
            return [str(Path(workspace) / str(value["subpath"]))]
        if keys == {"roots"}:
            return [
                str(Path(root).expanduser()) for root in value["roots"]
            ] or None
        raise ValueError(f"path_policy 声明键不合法: {sorted(keys)}")
    raise ValueError(
        f"path_policy 声明必须是 \"session\"、False 或字典: {value!r}"
    )


def declare_path_policy_tools(
    registry: Any,
    tools: Iterable[Any],
    *,
    resolve,
) -> list[str]:
    """Register path-policy declarations for tools that self-declare.

    A tool opts in with a ``path_policy`` class attribute ("session",
    {"subpath": ...}, {"roots": [...]} — see
    resolve_path_policy_declaration) — the declaration lands in the
    session ``CapabilityRegistry`` as data, so adding a governed tool
    never means editing the wiring code. Explicit declarations beat
    ``PathPolicyGuard``'s legacy fallback list. Returns the declared
    tool names.
    """
    from courtier.agent.core.capability import Capability

    declared: list[str] = []
    for tool in tools:
        name = getattr(tool, "name", None)
        value = getattr(tool, "path_policy", False)
        if not name or not value:
            continue
        roots = resolve(value)
        if not roots:
            continue
        registry.register(
            Capability(
                type="tool",
                name=name,
                meta={"permission": {"path_policy": {"roots": roots}}},
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

    def _managed_roots(self, tool_name: str) -> list[str] | None:
        """该工具生效的根列表；None = 不受管。

        判定顺序：显式 Capability 声明（播种时已把 "session"/{subpath}/
        {roots} 解析为具体 roots 字典）→ 未声明回退构造时的默认名单。
        """
        if self._capabilities is not None:
            capability = self._capabilities.get("tool", tool_name)
            permission = getattr(capability, "meta", {}).get("permission")
            if capability is not None and isinstance(permission, dict):
                if "path_policy" not in permission:
                    # 带权限字典却没写 path_policy：声明不完整，fail-closed
                    return []
                declaration = permission["path_policy"]
                if declaration is False:
                    return None                     # 显式豁免 → 不受管
                if isinstance(declaration, dict) and "roots" in declaration:
                    return [str(root) for root in declaration["roots"]]
                # 看不懂的声明 fail-closed：该工具所有路径拒绝
                return []
        if tool_name in self._path_tools:
            return [str(root) for root in self._roots]
        return None

    async def check_call(self, call: "ToolCall", context: Any) -> CallGuardResult:
        roots = self._managed_roots(call.name)
        # roots is None = 未受管 → 放行；roots == [] = 声明无法解析的
        # fail-closed 态 → 走 _check_path，空根清单拒绝一切路径。
        if not self._roots or roots is None:
            return CallGuardResult.allow(self.name)
        denial = self._check_path(call.name, (call.arguments or {}).get("path"), roots)
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
