"""Media probing tool — ffprobe metadata for uploaded audio/video files.

The plugin is deliberately LLM-free (host-side de-LLM principle): it only
decodes container/stream metadata with ``ffprobe``. Upload validation is
fail-closed host-side — a media file the plugin cannot decode never enters
the file index.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any

from courtier_plugin_sdk import ToolResult
from courtier_plugin_sdk.files import put_file, resolve_file


def _ffprobe_path() -> str:
    import os

    return os.environ.get("MEDIA_FFPROBE_PATH", "ffprobe")


def _probe_success(data: dict[str, Any]) -> bool:
    return data.get("success") is True


async def _communicate_and_reap(
    proc: asyncio.subprocess.Process, timeout: float
) -> tuple[bytes, bytes]:
    """Communicate with *proc* under *timeout*; on timeout or cancellation
    kill the child so no orphan ffmpeg keeps burning CPU after the caller
    has gone away."""
    try:
        return await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        raise


async def _run_ffprobe(path: Path) -> dict[str, Any]:
    """Run ffprobe and return its parsed JSON (raises on failure)."""
    proc = await asyncio.create_subprocess_exec(
        _ffprobe_path(),
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await _communicate_and_reap(proc, timeout=30.0)
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffprobe exit {proc.returncode}: {stderr.decode(errors='replace')[:300]}"
        )
    return json.loads(stdout.decode(errors="replace"))


def _summarize(probe: dict[str, Any]) -> dict[str, Any]:
    """Reduce ffprobe output to the fields the host file index stores."""
    fmt = probe.get("format") or {}
    streams = probe.get("streams") or []
    video = next(
        (s for s in streams if s.get("codec_type") == "video"), None
    )
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    duration: float | None = None
    for candidate in (fmt.get("duration"), (video or audio or {}).get("duration")):
        try:
            if candidate is not None:
                duration = float(candidate)
                break
        except (TypeError, ValueError):
            continue

    width = video.get("width") if isinstance(video, dict) else None
    height = video.get("height") if isinstance(video, dict) else None

    return {
        "success": True,
        "format_name": fmt.get("format_name", ""),
        "duration_seconds": duration,
        "width": width,
        "height": height,
        "video_codec": video.get("codec_name") if isinstance(video, dict) else None,
        "audio_codec": audio.get("codec_name") if isinstance(audio, dict) else None,
        "nb_streams": len(streams),
    }


class ProbeMediaTool:
    """Probe an uploaded media file's real container/stream metadata."""

    name: str = "probe_media"
    display_name: str | None = "媒体探测"
    # Host-only service (upload-time validation) — hidden from agents.
    internal: bool = True
    description: str = (
        "用 ffprobe 探测上传的音频/视频文件，返回真实格式名、时长（秒）、"
        "分辨率与编码。宿主用此结果校验媒体上传并执行时长限制。"
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "format": "file-ref",
                "description": "待探测媒体文件的路径（或 minio:// 引用）。",
            },
        },
        "required": ["file_path"],
    }

    async def execute(self, **kwargs: Any) -> ToolResult:
        file_path = kwargs.get("file_path")
        if not isinstance(file_path, str) or not file_path:
            return ToolResult(success=False, error="缺少必填参数 file_path")

        try:
            # File arguments arrive as minio:// references (the host rewrites
            # upload-dir paths at the proxy boundary); resolve_file downloads
            # into the per-request workdir. Plain local paths pass through.
            local = Path(await resolve_file(file_path)).resolve()
            if not local.is_file():
                return ToolResult(success=False, error=f"文件不存在: {file_path}")
            summary = _summarize(await _run_ffprobe(local))
        except FileNotFoundError:
            return ToolResult(
                success=False,
                error=(
                    "ffprobe 不可用：media 插件宿主需安装 ffmpeg（或通过 "
                    "MEDIA_FFPROBE_PATH 指定 ffprobe 路径）"
                ),
            )
        except Exception as exc:  # decode failures, timeouts, bad JSON
            return ToolResult(success=False, error=f"探测失败: {exc}")

        return ToolResult(success=True, data=summary)


def run_probe_standalone(path: str) -> dict[str, Any]:
    """Sync helper for tests/scripts: probe a local file without the SDK."""
    return _summarize(asyncio.run(_run_ffprobe(Path(path))))


__all__ = ["ProbeMediaTool", "run_probe_standalone", "_probe_success"]


def _transcode_timeout() -> float:
    import os

    return float(os.environ.get("MEDIA_TRANSCODE_TIMEOUT", "570"))


def _transcode_max_edge() -> int:
    import os

    return int(os.environ.get("MEDIA_TRANSCODE_MAX_EDGE", "1920"))


def _transcode_fps() -> str:
    """Constant output fps — low on purpose: model-side processors sample
    frames off the container metadata, so a 30fps screen recording balloons
    into hundreds of vision tokens per second and stalls prefill. 2fps is
    the Qwen video-understanding sampling rate."""
    import os

    return os.environ.get("MEDIA_TRANSCODE_FPS", "2")


class TranscodeVideoTool:
    """Re-encode a non-mp4 video into a provider-friendly mp4 (h264/aac).

    Screen-recordings often carry broken container fps metadata (e.g. VP8
    webm read as 1000fps) that crashes model-side video processors even
    though decoding succeeds; a clean mp4 re-encode fixes both the container
    and the metadata. The transcoded file is uploaded to the transfer bucket
    (put_file) and returned as a minio:// reference for the host to fetch.
    """

    name: str = "transcode_video"
    display_name: str | None = "视频转码"
    description: str = (
        "用 ffmpeg 将上传视频转码为 mp4（h264 + aac，限制长边，恒定低帧率，"
        "默认 2fps）。耗时较长：宿主最长等待 call_timeout_seconds。"
        "返回输出的 minio:// 引用。"
    )
    internal: bool = True
    call_timeout_seconds: int = 600
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "format": "file-ref",
                "description": "源视频文件的路径（或 minio:// 引用）。",
            },
        },
        "required": ["file_path"],
    }

    async def execute(self, **kwargs: Any) -> ToolResult:
        file_path = kwargs.get("file_path")
        if not isinstance(file_path, str) or not file_path:
            return ToolResult(success=False, error="缺少必填参数 file_path")

        try:
            local = Path(await resolve_file(file_path)).resolve()
            if not local.is_file():
                return ToolResult(success=False, error=f"文件不存在: {file_path}")
            out_path = local.parent / f"{local.stem}_transcoded.mp4"
            max_edge = _transcode_max_edge()
            proc = await asyncio.create_subprocess_exec(
                _ffmpeg_path(),
                "-y", "-loglevel", "error",
                "-i", str(local),
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-vf", f"scale=min(iw\\,{max_edge}):-2",
                "-r", _transcode_fps(),
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart",
                str(out_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await _communicate_and_reap(proc, timeout=_transcode_timeout())
            if proc.returncode != 0:
                return ToolResult(
                    success=False,
                    error=f"ffmpeg 退出 {proc.returncode}: {stderr.decode(errors='replace')[:300]}",
                )
            ref = await put_file(out_path, filename=out_path.name, content_type="video/mp4")
        except FileNotFoundError:
            return ToolResult(
                success=False,
                error=(
                    "ffmpeg 不可用：media 插件宿主需安装 ffmpeg（或通过 "
                    "MEDIA_FFPROBE_PATH/MEDIA_FFMPEG_PATH 指定可执行文件）"
                ),
            )
        except asyncio.TimeoutError:
            return ToolResult(
                success=False,
                error=f"转码超时（上限 {int(_transcode_timeout())} 秒），请压缩视频后重试",
            )
        except Exception as exc:
            return ToolResult(success=False, error=f"转码失败: {exc}")

        return ToolResult(success=True, data={"success": True, "minio_ref": ref})


def _ffmpeg_path() -> str:
    import os

    return os.environ.get("MEDIA_FFMPEG_PATH", "ffmpeg")


__all__ = ["ProbeMediaTool", "TranscodeVideoTool", "run_probe_standalone", "_probe_success"]
