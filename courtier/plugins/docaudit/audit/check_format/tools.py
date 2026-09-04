"""Format audit tool — wraps validator.format_checker for JSON-RPC access."""

from __future__ import annotations

from typing import Any

from courtier_plugin_sdk import ToolResult
from validator.format_checker import validator


class FormatAuditTool:
    """Validate document format compliance."""

    name: str = "check_format"
    display_name: str | None = "格式审查"
    description: str = (
        "按 GB/T 9704-2012 规则校验已解析文档的格式。传入 parse_layout 工具"
        "输出的文档 dict，返回违规列表，含块名、错误类型、详情与页码。"
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "doc": {
                "type": "object",
                "description": "已解析的文档 dict（parse_layout 工具输出）。",
            },
            "doc_type": {
                "type": "string",
                "description": "文种（如 通知、函、请示）。",
                "default": "通知",
            },
        },
        "required": ["doc"],
    }

    async def execute(
        self, doc: dict[str, Any], doc_type: str = "通知", **kwargs: Any
    ) -> ToolResult:
        """Execute format audit."""
        try:
            result = validator(doc, doc_type=doc_type)
            # 按 summary.py 的 issue_counts 机制输出计数（err/warn 驱动
            # ToolSummary.status，整个 dict 透传给展示层）：
            # - err：错误条数；格式审查结果没有"警告"概念，warn 恒为 0；
            # - ok：文档级通过（无错误）记 1，否则 0（逐条"通过"计数
            #   validator 不产出，无法提供）；
            # - unchecked：未检查条数（文档侧字段未提取/来源不可靠被跳过
            #   的检查），机制无对应槽位，作为扩展键随 dict 透传。
            errors = result.get("errors") if isinstance(result, dict) else None
            unchecked = result.get("unchecked") if isinstance(result, dict) else None
            err_count = len(errors) if isinstance(errors, list) else 0
            unchecked_count = len(unchecked) if isinstance(unchecked, list) else 0
            issue_counts = {
                "err": err_count,
                "warn": 0,
                "ok": 1 if err_count == 0 else 0,
                "unchecked": unchecked_count,
            }
            return ToolResult(
                success=True,
                data=result,
                metadata={"issue_counts": issue_counts},
            )
        except Exception as exc:
            return ToolResult(success=False, error=str(exc))
