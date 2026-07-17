"""Content audit tool — wraps content_compliance checkers for JSON-RPC access."""

from __future__ import annotations

from typing import Any

from content_compliance import get_checker, init_checkers, list_supported_types

from courtier.agent.tools.protocol import ToolResult
from courtier.plugin.types import ComplianceResult


class ContentAuditTool:
    """Validate document body content against compliance rules."""

    name: str = "check_content"
    description: str = (
        "Check document body text against content compliance rules for a given "
        "document type. Returns a list of violations with rule IDs, messages, "
        "severity, and positions."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "Document body text to audit.",
            },
            "doc_type": {
                "type": "string",
                "description": "Document type (e.g. 通知, 请示, 报告).",
            },
            "subtype": {
                "type": "string",
                "description": "Document subtype (e.g. 发布性通知).",
            },
        },
        "required": ["text", "doc_type"],
    }

    def __init__(self) -> None:
        self._initialized = False

    def _ensure_initialized(self) -> None:
        if not self._initialized:
            init_checkers()
            self._initialized = True

    async def execute(
        self, text: str, doc_type: str, subtype: str = "", **kwargs: Any
    ) -> ToolResult:
        """Execute content compliance check."""
        try:
            self._ensure_initialized()
            checker = get_checker(doc_type)
            if checker is None:
                supported = list_supported_types()
                return ToolResult(
                    success=False,
                    error=(
                        f"No checker registered for doc_type '{doc_type}'. "
                        f"Supported types: {supported}"
                    ),
                )
            result: ComplianceResult = await checker.check(text, subtype or None)
            return ToolResult(
                success=True,
                data={
                    "is_valid": result.is_valid,
                    "violations": [
                        {
                            "rule_id": v.rule_id,
                            "message": v.message,
                            "severity": v.severity,
                            "position": v.position,
                        }
                        for v in result.violations
                    ],
                },
            )
        except Exception as exc:
            return ToolResult(success=False, error=str(exc))
