"""ParseTool — parse document files into Document model."""

from __future__ import annotations

import base64
import logging
from typing import Any

from courtier.agent.tools.protocol import ToolResult

_DROP_KEYS = frozenset(
    {
        "raw",
        "position",
        "exist",
        "block_no",
        "save_path",
        "space_before",
        "space_after",
        "line_spacing",
    }
)


def _sanitize(obj: Any) -> Any:
    """Recursively strip binary data and irrelevant fields for LLM context."""
    if isinstance(obj, (bytes, bytearray)):
        return f"<binary:{base64.b64encode(obj).decode()[:64]}...>"
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items() if k not in _DROP_KEYS}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v) for v in obj]
    return obj


class ParseTool:
    """Parse a document file (PDF/DOCX/image) into the Document model."""

    name: str = "parse_document"
    description: str = (
        "Parse a document file from disk into the internal Document model. "
        "Supports PDF, DOCX, and scanned images. Returns the parsed Document as a dict."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Absolute path to the document file to parse.",
            }
        },
        "required": ["file_path"],
    }

    output_schema: dict | None = {
        "type": "object",
        "properties": {
            "metadata": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "str"},
                    "doc_id": {"type": "str"},
                    "total_page_num": {"type": "int"},
                },
            },
            "pages": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "page_no": {"type": "int"},
                        "header": {"type": "object"},
                        "body": {"type": "array"},
                        "footer": {"type": "object"},
                    },
                },
            },
        },
    }

    output_artifact_type: str | None = "docaudit.parsed_document"

    def __init__(self) -> None:
        self._cache: dict[str, ToolResult] = {}

    async def execute(self, **kwargs: Any) -> ToolResult:
        try:
            import os
            from pathlib import Path

            raw_path = kwargs["file_path"]

            # Resolve the upload root from environment (prefer COURTIER_*, fall back
            # to legacy DOCAUDIT_* names for compatibility).
            safe_root_raw = os.environ.get("COURTIER_UPLOAD_DIR") or os.environ.get(
                "DOCAUDIT_UPLOAD_DIR"
            ) or os.environ.get("UPLOAD_DIR")
            if not safe_root_raw:
                return ToolResult(
                    success=False,
                    error="Upload directory not configured",
                )
            safe_root = Path(safe_root_raw).resolve()

            # Return cached result for same file path
            if raw_path in self._cache:
                return self._cache[raw_path]

            p = Path(raw_path)
            if p.is_absolute():
                resolved = p.resolve()
            else:
                resolved = (safe_root / p).resolve()

            # Reject paths outside the upload root
            try:
                resolved.relative_to(safe_root)
            except ValueError:
                logging.getLogger(__name__).warning(
                    "Path escape attempt blocked: %s", raw_path
                )
                return ToolResult(
                    success=False, error=f"Access denied: {raw_path}"
                )

            if not resolved.is_file():
                return ToolResult(
                    success=False,
                    error=f"File not found or not a regular file: {raw_path}",
                )

            from docparse import parse

            doc = parse(str(resolved))
            data = _sanitize(doc.model_dump())
            result = ToolResult(success=True, data=data)
            self._cache[raw_path] = result
            return result
        except Exception as exc:
            return ToolResult(success=False, error=str(exc))


def create_parse_tool() -> ParseTool:
    return ParseTool()
