"""ParseTool — parse document files into Document model."""

from __future__ import annotations

import asyncio
import base64
import copy
from pathlib import Path
from typing import Any

from courtier_plugin_sdk import ToolResult, resolve_file

# Keys stripped recursively from the dumped Document before it is returned.
# Position and spacing fields (position, space_before, space_after,
# line_spacing, first_indent, left_indent, right_indent) must be kept: the
# format validator reads them for its spacing checks, and the artifact
# projectors read the page_content header/body/footer slots from this output.
# Only genuinely internal fields may be dropped here:
# - save_path: host filesystem path — never leak it to callers
_DROP_KEYS = frozenset(
    {
        "save_path",
    }
)


def _sanitize(obj: Any) -> Any:
    """Recursively strip binary data and internal fields for LLM context."""
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
    display_name: str | None = "解析文档"
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

    # The host rewrites this argument into a minio:// reference before
    # dispatch; resolve_file() downloads it into the request workdir.
    file_params: list[str] = ["file_path"]

    output_schema: dict | None = {
        "type": "object",
        "properties": {
            "schema_version": {
                "type": "string",
                "description": 'Document model structure version (e.g. "1.0").',
            },
            "doc_id": {"type": "string"},
            "total_page_num": {"type": "integer"},
            "warnings": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Non-fatal parse warnings (e.g. OCR degradation), if any.",
            },
            "warnings_summary": {
                "type": "string",
                "description": (
                    "Human-readable summary of parse warnings, present only "
                    "when warnings is non-empty."
                ),
            },
            "pages": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "page_no": {"type": "integer"},
                        "page_content": {
                            "type": "object",
                            "properties": {
                                "header": {"type": "object"},
                                "body": {"type": "object"},
                                "footer": {"type": "object"},
                                "margin": {"type": "object"},
                            },
                        },
                    },
                },
            },
        },
    }

    output_artifact_type: str | None = "docaudit.parsed_document"

    _MAX_CACHE_SIZE = 64

    def __init__(self) -> None:
        # key: resolved absolute path -> ((mtime_ns, size) fingerprint, result)
        self._cache: dict[str, tuple[tuple[int, int], ToolResult]] = {}
        self._cache_keys: list[str] = []  # LRU tracking (most recent last)

    async def execute(self, **kwargs: Any) -> ToolResult:
        try:
            file_path = kwargs.get("file_path")
            if not isinstance(file_path, str) or not file_path:
                return ToolResult(
                    success=False,
                    error="缺少必填参数 file_path（文档绝对路径）",
                )

            # File arguments arrive as minio:// references (the host rewrites
            # upload-dir paths at the proxy boundary after enforcing the
            # sandbox there); resolve_file downloads into the per-request
            # workdir.  Plain local paths pass through (tests / direct calls).
            file_path = await resolve_file(file_path)
            resolved = Path(file_path).resolve()

            if not resolved.is_file():
                return ToolResult(
                    success=False,
                    error=f"File not found or not a regular file: {file_path}",
                )

            # Cache lookup happens only after the sandbox checks above. The key
            # is the normalized absolute path and the entry is invalidated by a
            # (mtime_ns, size) fingerprint, so overwriting the file forces a
            # re-parse instead of serving stale data.
            stat = resolved.stat()
            fingerprint = (stat.st_mtime_ns, stat.st_size)
            cache_key = str(resolved)
            cached = self._cache.get(cache_key)
            if cached is not None and cached[0] == fingerprint:
                # LRU: move to end
                self._cache_keys.remove(cache_key)
                self._cache_keys.append(cache_key)
                # Return a private copy so callers mutating the result cannot
                # pollute the cached entry.
                return copy.deepcopy(cached[1])

            from docparse import parse

            # docparse.parse is synchronous and can take minutes on scanned
            # files (OCR + multimodal LLM) — run it off the event loop so the
            # plugin keeps answering health checks and cancellation requests.
            doc = await asyncio.to_thread(parse, str(resolved))
            # exclude_none=True：None 槽位/间距不再序列化（下游 validator
            # 全用 .get() 取数、projectors 均 None 安全，缺失键与显式 None
            # 行为一致）。不用 exclude_defaults，以免丢掉 page_no=0、
            # font_weight=False 等有语义的默认值。
            data = _sanitize(doc.model_dump(exclude_none=True))
            # Surface non-fatal parse warnings on the visible layers: data
            # keeps ``warnings`` verbatim, while ``warnings_summary`` (data
            # top-level) and metadata["warnings"] still reach the model when
            # the payload is large enough to be persisted behind a $ref.
            metadata: dict[str, Any] = {}
            warnings = [w for w in doc.warnings if isinstance(w, str) and w.strip()]
            if warnings:
                preview = "；".join(warnings[:5])
                if len(warnings) > 5:
                    preview += f" 等（共 {len(warnings)} 条）"
                data["warnings_summary"] = f"解析警告 {len(warnings)} 条：{preview}"
                metadata["warnings"] = warnings
            result = ToolResult(success=True, data=data, metadata=metadata)
            # LRU eviction: remove oldest if at capacity
            if cache_key not in self._cache and len(self._cache) >= self._MAX_CACHE_SIZE:
                oldest = self._cache_keys.pop(0)
                self._cache.pop(oldest, None)
            # Store a private copy; every returned object stays caller-owned.
            self._cache[cache_key] = (fingerprint, copy.deepcopy(result))
            if cache_key in self._cache_keys:
                self._cache_keys.remove(cache_key)
            self._cache_keys.append(cache_key)
            return result
        except Exception as exc:
            return ToolResult(success=False, error=str(exc))


def create_parse_tool() -> ParseTool:
    return ParseTool()
