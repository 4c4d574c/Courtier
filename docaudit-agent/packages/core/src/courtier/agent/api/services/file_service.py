"""File service — upload handling extracted from routes."""

from __future__ import annotations

import asyncio
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import UploadFile

ALLOWED_EXTS = {
    ".pdf",
    ".docx",
    ".bmp",
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".tif",
    ".tiff",
}
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB

ALLOWED_MIME_TYPES: dict[str, set[str]] = {
    ".pdf": {"application/pdf"},
    ".docx": {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/wps-office.docx",  # WPS Office registers this MIME type
    },
    ".bmp": {"image/bmp"},
    ".jpg": {"image/jpeg"},
    ".jpeg": {"image/jpeg"},
    ".png": {"image/png"},
    ".gif": {"image/gif"},
    ".tif": {"image/tiff"},
    ".tiff": {"image/tiff"},
}


async def upload_file(
    file: UploadFile, settings: Any, file_store: Any
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

    # Validate MIME type.  Browsers and proxies may send the generic
    # application/octet-stream instead of the official type; accept it
    # as a valid fallback for any extension.
    allowed_mimes = ALLOWED_MIME_TYPES.get(ext, set()) | {"application/octet-stream"}
    if file.content_type and file.content_type not in allowed_mimes:
        raise HTTPException(
            400,
            f"文件类型不匹配: 扩展名 {ext} 但 Content-Type 为 {file.content_type}",
        )

    contents = await file.read()
    if not contents:
        raise HTTPException(400, "文件内容为空")

    if len(contents) > MAX_FILE_SIZE:
        raise HTTPException(413, "文件过大")

    date_str = datetime.now().strftime("%Y/%m/%d")
    safe_name = f"courtier_{datetime.now().strftime('%Y%m%d%H%M%S')}_{secrets.token_hex(4)}{ext}"
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
    )

    return {"fileId": info.file_id}
