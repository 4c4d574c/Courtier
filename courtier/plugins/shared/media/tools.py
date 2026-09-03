"""Media probing tool — ffprobe metadata for uploaded audio/video files.

The plugin is deliberately LLM-free (host-side de-LLM principle): it only
decodes container/stream metadata with ``ffprobe``. Upload validation is
fail-closed host-side — a media file the plugin cannot decode never enters
the file index.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from courtier_plugin_sdk import ToolResult
from courtier_plugin_sdk.files import resolve_file


def _ffprobe_path() -> str:
    import os

    return os.environ.get("MEDIA_FFPROBE_PATH", "ffprobe")


def _probe_success(data: dict[str, Any]) -> bool:
    return data.get("success") is True


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
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30.0)
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
        "Probe an uploaded audio/video file with ffprobe. Returns real format "
        "name, duration (seconds), dimensions and codecs. The host uses this "
        "to validate media uploads and enforce duration limits."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "format": "file-ref",
                "description": "Path (or minio:// reference) of the media file to probe.",
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
