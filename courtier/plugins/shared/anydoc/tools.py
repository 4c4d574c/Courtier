"""ConvertDocumentTool — convert office documents into Markdown via anydoc."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from courtier_plugin_sdk import ToolResult

# The plugin runs entry.py as a script (sys.path[0] = plugin dir), where
# ``tools`` is a top-level module and relative imports fail; tests import it
# as ``plugins.shared.anydoc.tools``. Support both.
try:
    from . import ocr
except ImportError:  # script mode
    import ocr  # type: ignore[no-redef]

logger = logging.getLogger(__name__)


class ConvertDocumentTool:
    """Convert an office document file into GitHub-Flavored Markdown.

    Unlike parse_document (docaudit domain, GB/T 9704 Document model), this
    tool is format-agnostic and content-only: it covers the office formats
    the audit pipeline does not read (doc, xls/xlsx, ppt/pptx, ODF, RTF,
    EPUB, CSV) as well as DOCX and text-based PDF, and returns Markdown
    instead of a positional/format model.  Scanned/image-only files fall
    back to the OCR endpoint (ANYDOC_OCR_API_URL) when configured.
    """

    name: str = "convert_document"
    display_name: str | None = "转换文档"
    description: str = (
        "Convert an office document (Word, Excel, PowerPoint, OpenDocument, RTF, "
        "EPUB, CSV, or text-based PDF) from disk into GitHub-Flavored Markdown. "
        "Scanned PDFs and image files are OCR'd into Markdown when the OCR "
        "endpoint is configured. Returns the Markdown text and detected format."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Absolute path to the document file to convert.",
            }
        },
        "required": ["file_path"],
    }

    output_schema: dict | None = {
        "type": "object",
        "properties": {
            "markdown": {
                "type": "string",
                "description": "GitHub-Flavored Markdown rendering of the document.",
            },
            "format": {
                "type": "string",
                "description": "Detected source format (e.g. 'docx', 'pdf', 'csv').",
            },
            "ocr": {
                "type": "boolean",
                "description": "True when the output came from the OCR fallback path.",
            },
            "pages": {
                "type": "integer",
                "description": "Page count when the OCR fallback path was used.",
            },
        },
        "required": ["markdown", "format"],
    }

    output_artifact_type: str | None = "core.document_markdown"

    async def execute(self, **kwargs: Any) -> ToolResult:
        try:
            file_path = kwargs.get("file_path")
            if not isinstance(file_path, str) or not file_path:
                return ToolResult(
                    success=False,
                    error="缺少必填参数 file_path（文档绝对路径）",
                )

            # Same upload-root sandbox as parse_document: relative paths are
            # resolved against the upload dir, absolute paths must stay inside it.
            safe_root_raw = (
                os.environ.get("COURTIER_UPLOAD_DIR")
                or os.environ.get("DOCAUDIT_UPLOAD_DIR")
                or os.environ.get("UPLOAD_DIR")
            )
            if not safe_root_raw:
                return ToolResult(
                    success=False,
                    error="Upload directory not configured",
                )
            safe_root = Path(safe_root_raw).resolve()

            p = Path(file_path)
            resolved = p.resolve() if p.is_absolute() else (safe_root / p).resolve()
            try:
                resolved.relative_to(safe_root)
            except ValueError:
                logger.warning("Path escape attempt blocked: %s", file_path)
                return ToolResult(success=False, error=f"Access denied: {file_path}")

            if not resolved.is_file():
                return ToolResult(
                    success=False,
                    error=f"File not found or not a regular file: {file_path}",
                )

            # Plain images are not convertible by anydoc — OCR them directly.
            if resolved.suffix.lower() in ocr.IMAGE_EXTENSIONS:
                return await self._ocr_fallback(
                    resolved, fmt=resolved.suffix.lower().lstrip("."), page_count=1
                )

            import anydoc

            data = resolved.read_bytes()
            # Content-based detection first; CSV has no content marker, so
            # fall back to the extension (which also normalizes e.g. .pptm
            # to its canonical family name).
            fmt = anydoc.format_from_bytes(data) or anydoc.format_from_extension(resolved.suffix)
            if fmt is None:
                return ToolResult(
                    success=False,
                    error=f"无法识别的文件格式: {file_path}",
                )

            try:
                # Conversion is CPU-bound Rust measured in milliseconds; run
                # it off the event loop so the plugin keeps answering health
                # checks and cancellation requests.
                markdown = await asyncio.to_thread(anydoc.to_markdown_bytes, data, fmt)
            except anydoc.UnsupportedError as exc:
                # Scanned/image-only PDF — OCR fallback when configured.
                if not ocr.is_ocr_configured():
                    return ToolResult(
                        success=False,
                        error=(
                            f"不支持的格式或纯扫描/图片型文件（{exc}）。"
                            "扫描件请使用 parse_document。"
                        ),
                    )
                try:
                    markdown, page_count = await ocr.ocr_pdf(str(resolved))
                except RuntimeError as oexc:
                    return ToolResult(success=False, error=str(oexc))
                return ToolResult(
                    success=True,
                    data={
                        "markdown": markdown,
                        "format": "pdf",
                        "ocr": True,
                        "pages": page_count,
                    },
                )
            except anydoc.EncryptedError:
                return ToolResult(
                    success=False,
                    error="文件已加密或受密码保护，无法转换",
                )
            except anydoc.ConvertError as exc:
                # Malformed / ResourceLimit / MissingPart
                return ToolResult(success=False, error=f"文件无法转换: {exc}")

            return ToolResult(success=True, data={"markdown": markdown, "format": fmt})
        except Exception as exc:
            return ToolResult(success=False, error=str(exc))

    async def _ocr_fallback(self, resolved: Path, fmt: str, page_count: int) -> ToolResult:
        """OCR *resolved* via the configured endpoint and return the result.

        Returns an error pointing at parse_document when the OCR endpoint is
        not configured, or the OCR service fails.
        """
        if not ocr.is_ocr_configured():
            return ToolResult(
                success=False,
                error=(
                    f"{fmt} 文件需要 OCR 服务，但 ANYDOC_OCR_API_URL 未配置。"
                    "请配置 OCR 服务，或使用 parse_document。"
                ),
            )
        try:
            if page_count == 1 and resolved.suffix.lower() in ocr.IMAGE_EXTENSIONS:
                markdown = await asyncio.to_thread(ocr.ocr_image, str(resolved))
            else:
                markdown, page_count = await ocr.ocr_pdf(str(resolved))
        except RuntimeError as exc:
            return ToolResult(success=False, error=str(exc))
        return ToolResult(
            success=True,
            data={"markdown": markdown, "format": fmt, "ocr": True, "pages": page_count},
        )
