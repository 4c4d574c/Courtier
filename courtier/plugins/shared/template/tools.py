"""Template loading tool — loads format templates via the template_store host service."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from courtier_plugin_sdk import HostTemplateStore, ToolResult


class LoadTemplateTool:
    """Load a format template from the host's database."""

    name: str = "load_template"
    display_name: str | None = "加载模板"
    description: str = (
        "按文种（可选指定模板 ID）从数据库加载格式模板。返回模板 dict，"
        "未找到时返回 None。"
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "doc_type": {
                "type": "string",
                "description": "要加载模板的文种（如 通知）。",
            },
            "template_id": {
                "type": "integer",
                "description": (
                    "可选的指定模板 ID；省略时加载默认模板。"
                ),
            },
        },
        "required": ["doc_type"],
    }

    def __init__(self, host_client_getter: Callable[[], Any] | None = None) -> None:
        # Lazy getter: the host-service client only exists after the
        # registration handshake, i.e. after tool construction.
        self._host_client_getter = host_client_getter

    async def execute(self, **kwargs: Any) -> ToolResult:
        try:
            client = self._host_client_getter() if self._host_client_getter else None
            if client is None:
                return ToolResult(
                    success=False,
                    error=(
                        "template_store host service not available. "
                        "Declare host_services: [template_store] and "
                        "permissions: [read:templates] in plugin.yaml."
                    ),
                )
            store = HostTemplateStore(client)
            result = await store.get(
                doc_type=kwargs["doc_type"],
                template_id=kwargs.get("template_id"),
            )
            return ToolResult(success=True, data=result)
        except Exception as exc:
            return ToolResult(success=False, error=str(exc))
