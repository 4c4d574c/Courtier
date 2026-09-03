"""Media plugin tests: ffprobe probe tool (synthetic + real when ffprobe exists)."""

import asyncio
import io
import shutil
import wave
from pathlib import Path

import pytest

from .conftest import _ensure_plugin_path

_ensure_plugin_path("media")

from plugins.shared.media.entry import MediaPlugin  # noqa: E402
from plugins.shared.media.tools import (  # noqa: E402
    ProbeMediaTool,
    _summarize,
)

_FFPROBE = shutil.which("ffprobe")


def _wav_file(tmp_path: Path, seconds: float = 2.0) -> Path:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * int(8000 * seconds))
    p = tmp_path / "tone.wav"
    p.write_bytes(buf.getvalue())
    return p


class TestRegistration:
    def test_plugin_registers_probe_tool(self):
        plugin = MediaPlugin()
        plugin._setup_handlers()
        caps, system_prompt = plugin._collect_capabilities()
        tool_names = [c["name"] for c in caps if c["type"] == "tool"]
        assert "probe_media" in tool_names
        assert len(system_prompt) > 0

    def test_file_ref_param_declared(self):
        spec = ProbeMediaTool().parameters["properties"]["file_path"]
        assert spec["format"] == "file-ref"


class TestSummarize:
    def test_video_plus_audio(self):
        summary = _summarize(
            {
                "format": {"format_name": "mov,mp4,m4a", "duration": "12.5"},
                "streams": [
                    {"codec_type": "video", "codec_name": "h264", "width": 640, "height": 480},
                    {"codec_type": "audio", "codec_name": "aac"},
                ],
            }
        )
        assert summary["success"] is True
        assert summary["duration_seconds"] == 12.5
        assert summary["width"] == 640 and summary["height"] == 480
        assert summary["video_codec"] == "h264" and summary["audio_codec"] == "aac"
        assert summary["nb_streams"] == 2

    def test_audio_only_stream_duration_fallback(self):
        summary = _summarize(
            {
                "format": {"format_name": "wav"},
                "streams": [
                    {"codec_type": "audio", "codec_name": "pcm_s16le", "duration": "3.25"}
                ],
            }
        )
        assert summary["duration_seconds"] == 3.25
        assert summary["width"] is None and summary["height"] is None

    def test_unparseable_duration_stays_none(self):
        summary = _summarize(
            {"format": {"format_name": "wav", "duration": "N/A"}, "streams": []}
        )
        assert summary["duration_seconds"] is None


class TestProbeMediaTool:
    def test_missing_file_path(self):
        result = asyncio.run(ProbeMediaTool().execute())
        assert result.success is False
        assert "file_path" in (result.error or "")

    def test_garbage_bytes_fail_closed(self, tmp_path):
        p = tmp_path / "bad.wav"
        p.write_bytes(b"\x00" * 64)
        result = asyncio.run(ProbeMediaTool().execute(file_path=str(p)))
        assert result.success is False
        assert "探测失败" in (result.error or "")

    @pytest.mark.skipif(_FFPROBE is None, reason="ffprobe not installed")
    def test_real_wav_probe(self, tmp_path):
        wav = _wav_file(tmp_path)
        result = asyncio.run(ProbeMediaTool().execute(file_path=str(wav)))
        assert result.success is True
        assert result.data["format_name"] == "wav"
        assert abs(result.data["duration_seconds"] - 2.0) < 0.2
        assert result.data["audio_codec"] == "pcm_s16le"


class TestTranscodeVideoTool:
    """T-转码：非 mp4 视频重编码为 mp4（真机 ffmpeg，跳过无 ffmpeg 环境）。"""

    @staticmethod
    def _make_webm(tmp_path: Path, seconds: float = 2.0) -> Path:
        import subprocess

        p = tmp_path / "recording.webm"
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", f"testsrc=duration={seconds}:size=320x240:rate=8",
                "-c:v", "libvpx", str(p),
            ],
            check=True,
        )
        return p

    @pytest.mark.skipif(_FFPROBE is None, reason="ffprobe not installed")
    def test_webm_transcodes_to_mp4(self, tmp_path, monkeypatch):
        import asyncio

        from plugins.shared.media import tools as media_tools

        webm = self._make_webm(tmp_path)
        uploaded: dict = {}

        async def fake_put_file(local_path, *, filename=None, content_type=None):
            uploaded["path"] = Path(local_path)
            uploaded["filename"] = filename
            return f"minio://courtier-plugin-io/out/media/{filename}"

        monkeypatch.setattr(media_tools, "put_file", fake_put_file)

        result = asyncio.run(ProbeMediaToolTranscodeRunner().run(str(webm)))
        assert result.success is True
        assert result.data["minio_ref"].startswith("minio://courtier-plugin-io/")
        out = uploaded["path"]
        assert out.read_bytes()[4:8] == b"ftyp"  # ISO-BMFF mp4 container

    @pytest.mark.skipif(_FFPROBE is None, reason="ffprobe not installed")
    def test_transcode_timeout_reports_failure(self, tmp_path, monkeypatch):
        import asyncio

        from plugins.shared.media import tools as media_tools

        webm = self._make_webm(tmp_path)
        monkeypatch.setattr(media_tools, "_transcode_timeout", lambda: 0.001)
        result = asyncio.run(ProbeMediaToolTranscodeRunner().run(str(webm)))
        assert result.success is False
        assert "超时" in (result.error or "")


class ProbeMediaToolTranscodeRunner:
    """驱动 TranscodeVideoTool 的辅助（resolve_file 直通本地路径）。"""

    @staticmethod
    async def run(path: str):
        from plugins.shared.media.tools import TranscodeVideoTool

        return await TranscodeVideoTool().execute(file_path=path)

