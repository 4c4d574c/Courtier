"""Document annotation tool — wrap docannot for Word comment insertion."""

from __future__ import annotations

import asyncio
import logging
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from courtier_plugin_sdk import HostStorage, ToolResult, put_file, resolve_file
from courtier_plugin_sdk.files import parse_minio_ref

logger = logging.getLogger(__name__)

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class AnnotateDocumentTool:
    """Add Word comments to a DOCX file at keyword occurrences."""

    name: str = "annotate_document"
    display_name: str | None = "文档批注"
    description: str = (
        "在 DOCX 文档的每个关键词出现处添加 Word 批注。传入源 DOCX（base64 字节或"
        "文件路径）与 {keyword, comment} 规则列表。成功后批注文档存入对象存储，"
        "结果含 download_link（现成的 Markdown 超链接）——请原样向用户展示该链接"
        "供其点击下载，不要把预签名 URL 当纯文本粘贴。对象存储不可用时回退为"
        "本地 output_path。"
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "source": {
                "type": "string",
                "description": "源 DOCX 内容（base64 字节）或文件路径。",
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
                "description": "批注规则列表，每项为 {keyword, comment}。",
            },
            "keep_comments": {
                "type": "boolean",
                "description": "是否保留文档中已有批注，默认保留。",
                "default": True,
            },
        },
        "required": ["source", "rules"],
    }

    # The host rewrites this argument into a minio:// reference before
    # dispatch when it carries an upload-dir path (base64 passes through);
    # resolve_file() downloads references into the request workdir.
    file_params: list[str] = ["source"]

    def __init__(self, host_client_getter: Callable[[], Any] | None = None) -> None:
        # Lazy getter: the host-service client only exists after the
        # registration handshake, i.e. after tool construction.
        self._host_client_getter = host_client_getter

    async def execute(self, **kwargs: Any) -> ToolResult:
        try:
            from docannot._annotate import _try_base64, annotate
            from docannot._rule import Rule

            rules = [Rule(keyword=r["keyword"], comment=r["comment"]) for r in kwargs["rules"]]
            keep = kwargs.get("keep_comments", True)

            source = await resolve_file(kwargs["source"])
            # Mirror docannot's source typing: a string that decodes as
            # base64 is data; anything else is a filesystem path (downloaded
            # minio refs always are).  docannot only touches the filesystem
            # for path inputs; base64 sources get no sandbox.
            source_path = None
            if isinstance(source, str) and _try_base64(source) is None:
                source_path = Path(source)
            allowed_dirs = None
            if source_path is not None and source_path.is_file():
                allowed_dirs = [source_path.resolve().parent]

            # Full-document DOCX processing — off the event loop so the
            # plugin stays responsive (health checks, cancellation).
            result_bytes = await asyncio.to_thread(
                annotate,
                source,
                rules,
                keep_comments=keep,
                allowed_dirs=allowed_dirs,
            )

            input_name = source_path.stem if source_path is not None else "annotated"

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

            # Fallback: storage unavailable — write annotated bytes to a
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
        """Upload the annotated DOCX; returns a receipt with download_url.

        Primary path: direct transfer-bucket upload (put_file) + host-minted
        presigned URL (storage.presign_get).  Falls back to the legacy
        base64 host service (storage.put) when MinIO is not configured in
        the plugin environment, and to None when both fail.
        """
        try:
            with tempfile.NamedTemporaryFile(
                suffix=f"_{filename}", prefix="docaudit_", delete=False
            ) as tmp:
                tmp.write(data)
                tmp_path = Path(tmp.name)
            try:
                ref = await put_file(tmp_path, filename=filename, content_type=_DOCX_MIME)
            finally:
                tmp_path.unlink(missing_ok=True)
            client = self._host_client_getter() if self._host_client_getter else None
            if client is None:
                return None
            bucket, key = parse_minio_ref(ref)
            receipt = await HostStorage(client).presign_get(bucket, key)
            return {
                "download_url": receipt.get("download_url"),
                "object_key": key,
                "expires_in": receipt.get("expires_in"),
            }
        except RuntimeError:
            # MinIO env missing — legacy base64 path below.
            pass
        except Exception:
            logger.warning(
                "Direct transfer-bucket upload failed; trying storage.put", exc_info=True
            )

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
