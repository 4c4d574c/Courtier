"""Format audit tool — wraps validator.format_checker for JSON-RPC access."""

from __future__ import annotations

from typing import Any

from validator.format_checker import validator

from courtier.agent.tools.protocol import ToolResult


class FormatAuditTool:
    """Validate document format compliance."""

    name: str = "check_format"
    display_name: str | None = "格式审查"
    description: str = (
        "Check a parsed document's formatting against GB/T 9704-2012 rules. "
        "Takes a parsed document dict (from parse_document tool output) and returns "
        "a list of format violations with block names, error types, details, and page numbers."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "doc": {
                "type": "object",
                "description": "Parsed document dict (parse_document tool output).",
            },
            "doc_type": {
                "type": "string",
                "description": "Document type (e.g. 通知, 函, 请示).",
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
            return ToolResult(success=True, data=result)
        except Exception as exc:
            return ToolResult(success=False, error=str(exc))
