"""Memory tool — layered DB-backed memory via the memory_store host service.

The caller identity params (``_caller_*``) are filled by the host at the
dispatch boundary (``x-host-injected``); this tool only relays them to the
host service alongside the action arguments.  All permission decisions
happen host-side in MemoryService.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from courtier_plugin_sdk import ToolResult

_LIST_PREVIEW_CHARS = 160


class MemoryTool:
    """List / read / write / delete memory entries (global + own user layer)."""

    name: str = "memory"
    display_name: str | None = "记忆"
    description: str = (
        "长期记忆读写。list 列出条目（返回你的用户层与全局共享层，含内容预览，"
        "并附 known_domains=已加载领域包列表）；read 按 entry_id 或 title 取全文；"
        "write 按 title 创建或更新（默认写入你的用户层，layer='global' 仅管理员可用）；"
        "delete 按 entry_id 或 title 删除。"
        "domain 省略为通用（common）；用户偏好归 common，领域相关的口径/规则"
        "（如审核规则、验收标准）填 known_domains 中的领域包名（如 docaudit）。"
        "用户偏好请主动写入记忆（domain=common）。"
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "read", "write", "delete"],
                "description": "操作类型。",
            },
            "title": {
                "type": "string",
                "description": "条目标题（read/write/delete 寻址用；write 必填）。",
            },
            "content": {
                "type": "string",
                "description": "条目内容（write 必填，其余动作忽略）。",
            },
            "domain": {
                "type": "string",
                "description": (
                    "领域包归类：省略或 common=通用；填已加载领域包名（如 docaudit）为领域记忆。"
                ),
            },
            "layer": {
                "type": "string",
                "enum": ["user", "global"],
                "description": (
                    "write 目标层：user=你的用户层（默认）；global=全局共享层（仅管理员）。"
                ),
            },
            "entry_id": {
                "type": "integer",
                "description": "条目 ID（read/delete 可用它精确寻址，来自 list 结果）。",
            },
            "_caller_uid": {
                "type": "integer",
                "x-host-injected": "memory_caller",
                "description": "由宿主注入的调用者用户 ID。",
            },
            "_caller_name": {
                "type": "string",
                "x-host-injected": "memory_caller",
                "description": "由宿主注入的调用者用户名。",
            },
            "_caller_is_admin": {
                "type": "boolean",
                "x-host-injected": "memory_caller",
                "description": "由宿主注入的调用者管理员标志。",
            },
        },
        "required": ["action"],
    }

    def __init__(self, host_client_getter: Callable[[], Any] | None = None) -> None:
        # Lazy getter: the host-service client only exists after the
        # registration handshake, i.e. after tool construction.
        self._host_client_getter = host_client_getter

    def _preview(self, entry: dict[str, Any]) -> dict[str, Any]:
        shown = entry.get("content") or ""
        total = len(shown)
        if total > _LIST_PREVIEW_CHARS:
            shown = shown[:_LIST_PREVIEW_CHARS] + "…"
        return {
            "entry_id": entry.get("id"),
            "layer": entry.get("layer"),
            "domain": entry.get("domain"),
            "title": entry.get("title"),
            "preview": shown,
            "content_chars": total,
            "updated_at": entry.get("updatedAt"),
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        try:
            client = self._host_client_getter() if self._host_client_getter else None
            if client is None:
                return ToolResult(
                    success=False,
                    error=(
                        "memory_store host service not available. "
                        "Declare host_services: [memory_store] and permissions: "
                        "[read:memory, write:memory] in plugin.yaml."
                    ),
                )
            params: dict[str, Any] = {"action": kwargs.get("action")}
            for key in (
                "title",
                "content",
                "domain",
                "layer",
                "entry_id",
                "_caller_uid",
                "_caller_name",
                "_caller_is_admin",
            ):
                if kwargs.get(key) is not None:
                    params[key] = kwargs[key]
            result = await client.call("memory_store.call", params)
            if isinstance(result, dict) and result.get("ok") is False:
                return ToolResult(success=False, error=str(result.get("error") or "记忆操作被拒绝"))
            if isinstance(result, dict) and result.get("error") is not None and "code" in result:
                # host service _deny 形状：{"error": {"code", "message"}}
                return ToolResult(
                    success=False,
                    error=str((result.get("error") or {}).get("message") or "记忆服务错误"),
                )
            # list 结果做预览压缩（known_domains 原样透传），read/write/delete
            # 原样返回。
            if isinstance(result, dict) and {"user", "global"} <= set(result.keys()):
                compact: dict[str, Any] = {
                    layer: [self._preview(e) for e in (entries or [])]
                    for layer, entries in result.items()
                    if layer in ("user", "global")
                }
                total = sum(len(v) for v in compact.values())
                if result.get("known_domains"):
                    compact["known_domains"] = list(result["known_domains"])
                return ToolResult(
                    success=True,
                    data={**compact, "total": total},
                )
            return ToolResult(success=True, data=result)
        except Exception as exc:
            return ToolResult(success=False, error=str(exc))
