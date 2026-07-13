"""Document annotation tool — wrap docannot for Word comment insertion."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from courtier.agent.tools.protocol import ToolResult


class AnnotateDocumentTool:
    """Add Word comments to a DOCX file at keyword occurrences."""

    name: str = "annotate_document"
    description: str = (
        "Add Word comments to a DOCX document at every occurrence of specified "
        "keywords. Takes source DOCX bytes (base64) or file path and a list of "
        "{keyword, comment} rules. Returns the output file path and annotation summary."
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

    async def execute(self, **kwargs: Any) -> ToolResult:
        try:
            import os
            from docannot._annotate import annotate
            from docannot._rule import Rule

            rules = [
                Rule(keyword=r["keyword"], comment=r["comment"])
                for r in kwargs["rules"]
            ]
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

            # Write annotated bytes to a temp file so we don't return
            # raw bytes in the tool result (bytes are not JSON-serializable).
            source = kwargs["source"]
            input_name = Path(source).stem if isinstance(source, str) else "annotated"
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


def create_annotate_document_tool() -> AnnotateDocumentTool:
    return AnnotateDocumentTool()
