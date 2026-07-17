"""File service — upload handling extracted from routes."""

from __future__ import annotations

import asyncio
import json
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import UploadFile


def _load_upload_limits() -> dict[str, Any]:
    """Load upload limits from the shared repo config, with built-in fallback."""
    limits_path = Path(__file__).resolve().parents[3] / "shared" / "file-upload-limits.json"
    try:
        data: dict[str, Any] = json.loads(limits_path.read_text(encoding="utf-8"))
        return data
    except (OSError, json.JSONDecodeError):
        return {}


_limits = _load_upload_limits()

ALLOWED_EXTS = set(_limits.get("allowed_extensions", [
    ".pdf",
    ".docx",
    ".bmp",
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".tif",
    ".tiff",
]))
MAX_FILE_SIZE = int(_limits.get("max_file_size", 50 * 1024 * 1024))

ALLOWED_MIME_TYPES: dict[str, set[str]] = {
    ext: set(mimes)
    for ext, mimes in _limits.get("mime_types", {
        ".pdf": {"application/pdf"},
        ".docx": {
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/wps-office.docx",
        },
        ".bmp": {"image/bmp"},
        ".jpg": {"image/jpeg"},
        ".jpeg": {"image/jpeg"},
        ".png": {"image/png"},
        ".gif": {"image/gif"},
        ".tif": {"image/tiff"},
        ".tiff": {"image/tiff"},
    }).items()
}

# Magic-byte signatures used when the browser sends application/octet-stream.
# Each entry is a list of possible prefixes for the extension.
_MAGIC_BYTES: dict[str, list[bytes]] = {
    ".pdf": [b"%PDF"],
    ".docx": [b"PK\x03\x04"],  # DOCX is a ZIP archive
    ".bmp": [b"BM"],
    ".jpg": [b"\xff\xd8\xff"],
    ".jpeg": [b"\xff\xd8\xff"],
    ".png": [b"\x89PNG\r\n\x1a\n"],
    ".gif": [b"GIF87a", b"GIF89a"],
    ".tif": [b"II*\x00", b"MM\x00*"],
    ".tiff": [b"II*\x00", b"MM\x00*"],
}


def _content_matches_extension(ext: str, content: bytes) -> bool:
    """Validate file content magic bytes against the declared extension."""
    signatures = _MAGIC_BYTES.get(ext, [])
    if not signatures:
        return False
    return any(content.startswith(sig) for sig in signatures)


async def upload_file(
    file: UploadFile,
    settings: Any,
    file_store: Any,
    owner: str = "",
) -> dict[str, Any]:
    """Validate and persist an uploaded document file.

    Returns a dict with ``fileId`` on success.
    Raises HTTPException for validation failures.
    """
    from fastapi import HTTPException

    if not file.filename:
        raise HTTPException(400, "文件为空")

    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTS:
        raise HTTPException(400, f"不支持的文件格式: {ext}")

    contents = await file.read()
    if not contents:
        raise HTTPException(400, "文件内容为空")

    if len(contents) > MAX_FILE_SIZE:
        raise HTTPException(413, "文件过大")

    # Validate MIME type. Browsers and proxies may send the generic
    # application/octet-stream; in that case fall back to magic-byte validation.
    allowed_mimes = ALLOWED_MIME_TYPES.get(ext, set())
    if file.content_type and file.content_type not in allowed_mimes:
        if file.content_type == "application/octet-stream":
            if not _content_matches_extension(ext, contents):
                raise HTTPException(
                    400,
                    f"文件类型不匹配: 扩展名 {ext} 但 Content-Type 为 "
                    f"{file.content_type} 且文件头校验失败",
                )
        else:
            raise HTTPException(
                400,
                f"文件类型不匹配: 扩展名 {ext} 但 Content-Type 为 {file.content_type}",
            )

    now = datetime.now()
    date_str = now.strftime("%Y/%m/%d")
    safe_name = f"courtier_{now.strftime('%Y%m%d%H%M%S')}_{secrets.token_hex(4)}{ext}"
    relative_path = f"{date_str}/{safe_name}"

    upload_root = Path(settings.upload_dir).resolve()
    target_dir = upload_root / date_str
    target_dir.mkdir(parents=True, exist_ok=True)
    full_path = (target_dir / safe_name).resolve()
    if not full_path.is_relative_to(upload_root):
        raise HTTPException(400, "非法文件路径")
    await asyncio.to_thread(full_path.write_bytes, contents)

    info = await file_store.register(
        original_name=file.filename,
        stored_path=relative_path,
        size_bytes=len(contents),
        owner=owner,
    )

    return {"fileId": info.file_id}
