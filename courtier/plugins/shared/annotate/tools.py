"""Document annotation tool — wrap docannot for Word comment insertion."""

from __future__ import annotations

import logging
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from courtier_plugin_sdk import HostStorage, ToolResult

logger = logging.getLogger(__name__)

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class AnnotateDocumentTool:
    """Add Word comments to a DOCX file at keyword occurrences."""

    name: str = "annotate_document"
    display_name: str | None = "文档批注"
    description: str = (
        "Add Word comments to a DOCX document at every occurrence of specified "
        "keywords. Takes source DOCX bytes (base64) or file path and a list of "
        "{keyword, comment} rules. On success the annotated document is stored "
        "in object storage and the result contains download_link (a ready-made "
        "Markdown hyperlink) — present that link to the user as-is so they can "
        "click to download; never paste the raw presigned URL as plain text. "
        "Falls back to a local output_path when storage is unavailable."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "source": {
                "type": "string",
                "description": "Raw DOCX bytes (base64) or file path.",
            },
            "rules": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "keyword": {"type": "string"},
                        "comment": {"type": "string"},
                    },
                    "required": ["keyword", "comment"],
                },
                "description": "List of {keyword, comment} pairs for annotation.",
            },
            "keep_comments": {
                "type": "boolean",
                "description": "Preserve existing comments in the document.",
                "default": True,
            },
        },
        "required": ["source", "rules"],
    }

    def __init__(self, host_client_getter: Callable[[], Any] | None = None) -> None:
        # Lazy getter: the host-service client only exists after the
        # registration handshake, i.e. after tool construction.
        self._host_client_getter = host_client_getter

    async def execute(self, **kwargs: Any) -> ToolResult:
        try:
            import os

            from docannot._annotate import annotate
            from docannot._rule import Rule

            rules = [Rule(keyword=r["keyword"], comment=r["comment"]) for r in kwargs["rules"]]
            keep = kwargs.get("keep_comments", True)

            upload_dir = Path(
                os.environ.get("COURTIER_UPLOAD_DIR")
                or os.environ.get("DOCAUDIT_UPLOAD_DIR")
                or os.environ.get("UPLOAD_DIR")
                or ""
            )
            allowed_dirs = [upload_dir.resolve()] if upload_dir else None

            result_bytes = annotate(
                kwargs["source"],
                rules,
                keep_comments=keep,
                allowed_dirs=allowed_dirs,
            )

            source = kwargs["source"]
            input_name = Path(source).stem if isinstance(source, str) else "annotated"

            stored = await self._store_output(f"{input_name}_annotated.docx", result_bytes)
            if stored is not None:
                download_url = stored.get("download_url")
                filename = f"{input_name}_annotated.docx"
                return ToolResult(
                    success=True,
                    data={
                        "download_link": f"[下载带批注的文档（{filename}）]({download_url})",
                        "download_url": download_url,
                        "object_key": stored.get("object_key"),
                        "expires_in": stored.get("expires_in"),
                        "size_bytes": len(result_bytes),
                        "rules_applied": len(rules),
                        "keywords": [r["keyword"] for r in kwargs["rules"]],
                    },
                )

            # Fallback: host storage unavailable — write annotated bytes to a
            # temp file so we don't return raw bytes in the tool result
            # (bytes are not JSON-serializable).
            with tempfile.NamedTemporaryFile(
                suffix=f"_{input_name}_annotated.docx",
                prefix="docaudit_",
                delete=False,
            ) as tmp:
                tmp.write(result_bytes)
                output_path = tmp.name

            return ToolResult(
                success=True,
                data={
                    "output_path": str(output_path),
                    "size_bytes": len(result_bytes),
                    "rules_applied": len(rules),
                    "keywords": [r["keyword"] for r in kwargs["rules"]],
                },
            )
        except Exception as exc:
            return ToolResult(success=False, error=str(exc))

    async def _store_output(self, filename: str, data: bytes) -> dict[str, Any] | None:
        """Upload via the host storage service; None when unavailable/failed."""
        if self._host_client_getter is None:
            return None
        client = self._host_client_getter()
        if client is None:
            return None
        try:
            return await HostStorage(client).put(filename, data, _DOCX_MIME)
        except Exception:
            logger.warning(
                "Host storage upload failed; falling back to temp file",
                exc_info=True,
            )
            return None


def create_annotate_document_tool(
    host_client_getter: Callable[[], Any] | None = None,
) -> AnnotateDocumentTool:
    return AnnotateDocumentTool(host_client_getter=host_client_getter)
