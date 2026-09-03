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
    """Load upload limits from the shared repo config, with built-in fallback.

    The config lives in ``<repo>/shared/file-upload-limits.json`` (shared
    with the frontend mirror); walk upward so the path survives repo
    relocations. Production images COPY shared/ to the same layout.
    """
    here = Path(__file__).resolve()
    for base in here.parents:
        candidate = base / "shared" / "file-upload-limits.json"
        if candidate.is_file():
            try:
                return json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                break
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
    ".mp3",
    ".wav",
    ".m4a",
    ".flac",
    ".mp4",
    ".mov",
    ".webm",
    ".mkv",
    ".avi",
]))
MAX_FILE_SIZE = int(_limits.get("max_file_size", 50 * 1024 * 1024))
# Per-kind caps (≤ MAX_FILE_SIZE); documents keep the global default.
KIND_LIMITS: dict[str, int] = {
    str(k): int(v)
    for k, v in _limits.get(
        "kind_limits",
        {"document": 52428800, "image": 20971520, "audio": 26214400, "video": 52428800},
    ).items()
}

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

# Extension → attachment kind. Drives per-kind size caps, probe dispatch,
# and the run-attachment gates; unknown extensions count as "document".
_IMAGE_EXTS = {".bmp", ".jpg", ".jpeg", ".png", ".gif", ".tif", ".tiff"}
_AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".flac"}
_VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".avi"}
MEDIA_KINDS = ("image", "audio", "video")
# Kinds the media plugin must decode before the upload is accepted. Images
# are static bytes the vision model consumes directly — no probe, no
# plugin dependency.
PROBED_KINDS = ("audio", "video")


def infer_kind(name: str) -> str:
    """Attachment kind for a filename; falls back to "document"."""
    ext = Path(name).suffix.lower()
    if ext in _IMAGE_EXTS:
        return "image"
    if ext in _AUDIO_EXTS:
        return "audio"
    if ext in _VIDEO_EXTS:
        return "video"
    return "document"


def kind_size_limit(kind: str) -> int:
    """Effective size cap for a kind (falls back to the global limit)."""
    return KIND_LIMITS.get(kind, MAX_FILE_SIZE)


# 每条消息各媒体 kind 的数量上限（图片可多张，音视频各一份）。
def _kind_counts(settings: Any) -> dict[str, int]:
    return {
        "image": int(getattr(settings, "media_max_images_per_message", 4)),
        "audio": 1,
        "video": 1,
    }


def gate_media_attachments(infos: list[Any], settings: Any) -> None:
    """Validate a run's media attachments against per-kind count/size/duration caps.

    Raises ``HTTPException(400/413)`` — no silent trimming; the caller has
    already checked existence and ownership.
    """
    from fastapi import HTTPException

    counts: dict[str, int] = {}
    limits = _kind_counts(settings)
    for info in infos:
        kind = info.kind or infer_kind(info.original_name)
        counts[kind] = counts.get(kind, 0) + 1
        limit = limits.get(kind)
        if limit is not None and counts[kind] > limit:
            raise HTTPException(400, f"附件数量超限：{kind} 每条消息最多 {limit} 个")
        cap = kind_size_limit(kind)
        if info.size_bytes > cap:
            raise HTTPException(413, f"文件过大（{kind} 类型上限 {cap // (1024 * 1024)} MB）")

    seconds_cap = {
        "audio": getattr(settings, "media_max_audio_seconds", None),
        "video": getattr(settings, "media_max_video_seconds", None),
    }
    for item in infos:
        kind = item.kind or infer_kind(item.original_name)
        cap = seconds_cap.get(kind)
        item_duration = getattr(item, "duration_seconds", None)
        if cap is not None and item_duration is not None and item_duration > cap:
            raise HTTPException(
                400, f"媒体时长超限：{kind} 上限 {int(cap)} 秒，实际 {int(item_duration)} 秒"
            )


# Magic-byte signatures used when the browser sends application/octet-stream.
# Each entry is a list of (offset, prefix) pairs for the extension.
_MAGIC_BYTES: dict[str, list[tuple[int, bytes]]] = {
    ".pdf": [(0, b"%PDF")],
    ".docx": [(0, b"PK\x03\x04")],  # DOCX is a ZIP archive
    ".bmp": [(0, b"BM")],
    ".jpg": [(0, b"\xff\xd8\xff")],
    ".jpeg": [(0, b"\xff\xd8\xff")],
    ".png": [(0, b"\x89PNG\r\n\x1a\n")],
    ".gif": [(0, b"GIF87a"), (0, b"GIF89a")],
    ".tif": [(0, b"II*\x00"), (0, b"MM\x00*")],
    ".tiff": [(0, b"II*\x00"), (0, b"MM\x00*")],
    ".mp3": [(0, b"ID3"), (0, b"\xff\xfb"), (0, b"\xff\xf3"), (0, b"\xff\xf2")],
    ".wav": [(0, b"RIFF")],
    ".flac": [(0, b"fLaC")],
    # ISO-BMFF family (mp4/m4a/mov): brand box starts at offset 4.
    ".mp4": [(4, b"ftyp")],
    ".m4a": [(4, b"ftyp")],
    ".mov": [(4, b"ftyp")],
    ".webm": [(0, b"\x1a\x45\xdf\xa3")],  # EBML
    ".mkv": [(0, b"\x1a\x45\xdf\xa3")],
    ".avi": [(0, b"RIFF")],
}


def _content_matches_extension(ext: str, content: bytes) -> bool:
    """Validate file content magic bytes against the declared extension."""
    signatures = _MAGIC_BYTES.get(ext, [])
    if not signatures:
        return False
    return any(content[offset:].startswith(sig) for offset, sig in signatures)


async def probe_media_file(
    file_path: Path,
    tool_registry: Any,
) -> dict[str, Any] | None:
    """Probe a stored media file via the media plugin's ``probe_media`` tool.

    Returns the plugin metadata dict, ``None`` when the plugin/tool is not
    registered, and raises ``HTTPException(400)`` when the plugin answers
    with a failure (corrupt/undecodable media) — media uploads are
    fail-closed; document uploads never reach this path.
    """
    from fastapi import HTTPException

    if tool_registry is None:
        return None
    try:
        tool = tool_registry.get("probe_media")
    except KeyError:
        return None

    result = await tool.execute(on_progress=lambda _progress: None, file_path=str(file_path))
    if not result.success:
        raise HTTPException(400, f"媒体文件探测失败: {result.error}")
    data = result.data if isinstance(result.data, dict) else {}
    return data


def _needs_video_transcode(probe: dict[str, Any]) -> bool:
    """True when a probed video is not an mp4 container (needs re-encode)."""
    fmt = str(probe.get("format_name", ""))
    return "mp4" not in fmt.lower()


async def transcode_video_file(
    file_path: Path,
    tool_registry: Any,
) -> bytes:
    """Transcode a stored video to mp4 via the media plugin.

    The plugin re-encodes (h264/aac, capped long edge, constant 30fps),
    uploads the result to the transfer bucket, and returns a minio:// ref;
    the host reads the object with its own (full-permission) credentials.
    Raises HTTPException(400) on any failure — media uploads fail closed.
    """
    from fastapi import HTTPException

    if tool_registry is None:
        raise HTTPException(400, "媒体上传不可用：media 插件未连接")
    try:
        tool = tool_registry.get("transcode_video")
    except KeyError:
        raise HTTPException(400, "媒体上传不可用：media 插件缺少转码工具") from None

    result = await tool.execute(on_progress=lambda _p: None, file_path=str(file_path))
    if not result.success:
        raise HTTPException(400, f"视频转码失败: {result.error}")
    data = result.data if isinstance(result.data, dict) else {}
    ref = data.get("minio_ref")
    if not isinstance(ref, str) or not ref.startswith("minio://"):
        raise HTTPException(400, "视频转码结果无效")

    from courtier.storage import client as storage_client

    parts = ref.removeprefix("minio://").split("/", 1)
    if len(parts) != 2:
        raise HTTPException(400, "视频转码结果无效")
    bucket, key = parts
    try:
        return await asyncio.to_thread(storage_client.get_object, bucket, key)
    except Exception as exc:
        raise HTTPException(400, f"转码结果取回失败: {exc}") from exc


async def upload_file(
    file: UploadFile,
    settings: Any,
    file_store: Any,
    owner: str = "",
    tool_registry: Any = None,
) -> dict[str, Any]:
    """Validate and persist an uploaded document file.

    Returns a dict with ``fileId`` (and kind/probe metadata when present).
    Raises HTTPException for validation failures.
    """
    from fastapi import HTTPException

    if not file.filename:
        raise HTTPException(400, "文件为空")

    filename = file.filename
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTS:
        raise HTTPException(400, f"不支持的文件格式: {ext}")
    kind = infer_kind(filename)

    contents = await file.read()
    if not contents:
        raise HTTPException(400, "文件内容为空")

    kind_cap = kind_size_limit(kind)
    if len(contents) > MAX_FILE_SIZE:
        raise HTTPException(413, "文件过大")
    if len(contents) > kind_cap:
        raise HTTPException(413, "文件过大（{}类型上限 {} MB）".format(kind, kind_cap // (1024 * 1024)))

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

    # Audio/video uploads probe through the media plugin before being
    # accepted — a file the plugin cannot decode is rejected here
    # (fail-closed), and the real duration lands in the file index for the
    # run gates. Images skip probing entirely.
    probe: dict[str, Any] | None = None
    if kind in PROBED_KINDS:
        try:
            probe = await probe_media_file(full_path, tool_registry)
        except HTTPException:
            await asyncio.to_thread(full_path.unlink, True)
            raise
        if probe is None:
            await asyncio.to_thread(full_path.unlink, True)
            raise HTTPException(400, "媒体上传不可用：media 插件未连接")

        # Non-mp4 videos are uniformly transcoded to mp4 (h264/aac) by the
        # media plugin: model-side video processors choke on containers with
        # broken fps metadata (e.g. VP8 screen recordings read as 1000fps).
        if kind == "video" and _needs_video_transcode(probe):
            transcode = await transcode_video_file(full_path, tool_registry)
            await asyncio.to_thread(full_path.unlink, True)
            date_dir = target_dir
            safe_mp4 = f"{safe_name.rsplit('.', 1)[0]}.mp4"
            full_path = (date_dir / safe_mp4).resolve()
            if not full_path.is_relative_to(upload_root):
                raise HTTPException(400, "非法文件路径")
            await asyncio.to_thread(full_path.write_bytes, transcode)
            relative_path = f"{date_str}/{safe_mp4}"
            try:
                probe = await probe_media_file(full_path, tool_registry)
            except HTTPException:
                await asyncio.to_thread(full_path.unlink, True)
                raise
            if probe is None:
                await asyncio.to_thread(full_path.unlink, True)
                raise HTTPException(400, "媒体上传不可用：media 插件未连接")

    def _probe_int(key: str) -> int | None:
        value = (probe or {}).get(key)
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    info = await file_store.register(
        original_name=filename,
        stored_path=relative_path,
        size_bytes=len(contents),
        owner=owner,
        kind=kind,
        duration_seconds=(probe or {}).get("duration_seconds"),
        width=_probe_int("width"),
        height=_probe_int("height"),
    )

    return {
        "fileId": info.file_id,
        "kind": info.kind,
        "durationSeconds": info.duration_seconds,
        "width": info.width,
        "height": info.height,
    }
