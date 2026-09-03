"""Media upload path: kind inference, per-kind caps, magic bytes, probe wiring."""

import asyncio
import io
import wave
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.datastructures import Headers, UploadFile

from courtier.agent.api.file_store import FileStore
from courtier.agent.api.services import file_service
from courtier.agent.api.services.file_service import (
    _content_matches_extension,
    gate_media_attachments,
    infer_kind,
    kind_size_limit,
    upload_file,
)
from courtier.agent.tools.protocol import ToolResult


def _wav_bytes(seconds: float = 2.0, rate: int = 8000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


def _upload_file(data: bytes, name: str, content_type: str) -> UploadFile:
    return UploadFile(
        file=io.BytesIO(data),
        filename=name,
        headers=Headers({"content-type": content_type}),
    )


class _StubProbeTool:
    def __init__(self, result: ToolResult):
        self.result = result
        self.calls: list[dict] = []

    async def execute(self, *, on_progress=None, **kwargs):
        self.calls.append(kwargs)
        return self.result


def _registry(tool: object | None) -> SimpleNamespace:
    def get(name: str):
        if tool is None or name != "probe_media":
            raise KeyError(name)
        return tool

    return SimpleNamespace(get=get)


class TestInferKind:
    @pytest.mark.parametrize(
        ("name", "kind"),
        [
            ("a.pdf", "document"),
            ("a.docx", "document"),
            ("a.png", "image"),
            ("a.JPG", "image"),
            ("a.tif", "image"),
            ("a.mp3", "audio"),
            ("a.WAV", "audio"),
            ("a.m4a", "audio"),
            ("a.flac", "audio"),
            ("a.mp4", "video"),
            ("a.mov", "video"),
            ("a.webm", "video"),
            ("a.mkv", "video"),
            ("a.avi", "video"),
        ],
    )
    def test_mapping(self, name, kind):
        assert infer_kind(name) == kind

    def test_unknown_is_document(self):
        assert infer_kind("a.xyz") == "document"


class TestKindLimits:
    def test_defaults_from_shared_config(self):
        assert kind_size_limit("audio") == 26214400
        assert kind_size_limit("image") == 20971520
        assert kind_size_limit("video") == file_service.MAX_FILE_SIZE
        assert kind_size_limit("document") == file_service.MAX_FILE_SIZE


class TestMagicBytes:
    @pytest.mark.parametrize(
        ("ext", "content"),
        [
            (".mp3", b"ID3\x04" + b"\x00" * 20),
            (".mp3", b"\xff\xfb\x90\x00"),
            (".wav", b"RIFFxxxxWAVEfmt "),
            (".flac", b"fLaC\x00\x00"),
            (".mp4", b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 8),
            (".m4a", b"\x00\x00\x00\x20ftypM4A "),
            (".mov", b"\x00\x00\x00\x14ftypqt  "),
            (".webm", b"\x1a\x45\xdf\xa3" + b"\x00" * 8),
            (".mkv", b"\x1a\x45\xdf\xa3" + b"\x00" * 8),
            (".avi", b"RIFFxxxxAVI "),
        ],
    )
    def test_matches(self, ext, content):
        assert _content_matches_extension(ext, content)

    def test_mismatch(self):
        assert not _content_matches_extension(".wav", b"\x89PNG\r\n\x1a\n")
        assert not _content_matches_extension(".mp4", b"RIFFxxxx")


class TestMediaUpload:
    def test_wav_upload_probes_and_persists_metadata(self, tmp_path):
        store = FileStore(str(tmp_path / "files"))
        probe = _StubProbeTool(
            ToolResult(
                success=True,
                data={
                    "success": True,
                    "format_name": "wav",
                    "duration_seconds": 2.0,
                    "width": None,
                    "height": None,
                    "audio_codec": "pcm_s16le",
                },
            )
        )
        info = asyncio.run(
            upload_file(
                _upload_file(_wav_bytes(), "tone.wav", "audio/wav"),
                SimpleNamespace(upload_dir=str(tmp_path / "uploads")),
                store,
                owner="u",
                tool_registry=_registry(probe),
            )
        )
        assert info["kind"] == "audio"
        assert info["durationSeconds"] == 2.0
        stored = asyncio.run(store.resolve(info["fileId"]))
        assert stored is not None
        assert stored.kind == "audio"
        assert stored.duration_seconds == 2.0
        # probe received an absolute path inside the upload dir
        assert probe.calls[0]["file_path"].endswith(".wav")

    def test_wav_upload_fail_closed_without_plugin(self, tmp_path):
        import asyncio

        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                upload_file(
                    _upload_file(_wav_bytes(), "tone.wav", "audio/wav"),
                    SimpleNamespace(upload_dir=str(tmp_path / "uploads")),
                    FileStore(str(tmp_path / "files")),
                    owner="u",
                    tool_registry=None,
                )
            )
        assert exc.value.status_code == 400
        assert "media 插件未连接" in exc.value.detail

    def test_wav_upload_fail_closed_on_probe_error(self, tmp_path):
        import asyncio

        probe = _StubProbeTool(ToolResult(success=False, error="decode boom"))
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                upload_file(
                    _upload_file(_wav_bytes(), "tone.wav", "audio/wav"),
                    SimpleNamespace(upload_dir=str(tmp_path / "uploads")),
                    FileStore(str(tmp_path / "files")),
                    owner="u",
                    tool_registry=_registry(probe),
                )
            )
        assert exc.value.status_code == 400
        assert "decode boom" in exc.value.detail

    def test_document_upload_skips_probe(self, tmp_path):
        import asyncio

        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        result = asyncio.run(
            upload_file(
                _upload_file(png, "doc.png", "image/png"),
                SimpleNamespace(upload_dir=str(tmp_path / "uploads")),
                FileStore(str(tmp_path / "files")),
                owner="u",
                tool_registry=None,  # images are not probed
            )
        )
        assert result["kind"] == "image"

    def test_audio_kind_cap_enforced(self, tmp_path, monkeypatch):
        import asyncio

        monkeypatch.setitem(file_service.KIND_LIMITS, "audio", 16)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                upload_file(
                    _upload_file(_wav_bytes(), "tone.wav", "audio/wav"),
                    SimpleNamespace(upload_dir=str(tmp_path / "uploads")),
                    FileStore(str(tmp_path / "files")),
                    owner="u",
                    tool_registry=None,
                )
            )
        assert exc.value.status_code == 413

    def test_probe_failure_removes_stored_bytes(self, tmp_path):
        import asyncio
        from pathlib import Path

        uploads = tmp_path / "uploads"
        probe = _StubProbeTool(ToolResult(success=False, error="bad"))
        with pytest.raises(HTTPException):
            asyncio.run(
                upload_file(
                    _upload_file(_wav_bytes(), "tone.wav", "audio/wav"),
                    SimpleNamespace(upload_dir=str(uploads)),
                    FileStore(str(tmp_path / "files")),
                    owner="u",
                    tool_registry=_registry(probe),
                )
            )
        assert not any(uploads.rglob("*.wav"))


class TestGateMediaAttachments:
    """运行前附件门控：数量/大小/时长（T4）。"""

    @staticmethod
    def _info(kind: str, size: int = 100, duration: float | None = None):
        from types import SimpleNamespace as NS

        return NS(kind=kind, size_bytes=size, duration_seconds=duration,
                  original_name=f"x.{kind}")

    @staticmethod
    def _settings(**kw):
        from types import SimpleNamespace as NS

        base = {"media_max_images_per_message": 4,
                "media_max_audio_seconds": 1800.0,
                "media_max_video_seconds": 300.0}
        base.update(kw)
        return NS(**base)

    def test_passes_within_limits(self):
        gate_media_attachments(
            [self._info("image"), self._info("image"), self._info("audio", duration=60.0)],
            self._settings(),
        )

    def test_image_count_cap(self):
        infos = [self._info("image") for _ in range(5)]
        with pytest.raises(HTTPException) as exc:
            gate_media_attachments(infos, self._settings())
        assert exc.value.status_code == 400
        assert "最多 4 个" in exc.value.detail

    def test_single_audio_video(self):
        with pytest.raises(HTTPException):
            gate_media_attachments(
                [self._info("video"), self._info("video")], self._settings()
            )
        with pytest.raises(HTTPException):
            gate_media_attachments(
                [self._info("audio"), self._info("audio")], self._settings()
            )

    def test_size_cap(self):
        with pytest.raises(HTTPException) as exc:
            gate_media_attachments(
                [self._info("image", size=99_000_000)], self._settings()
            )
        assert exc.value.status_code == 413

    def test_duration_cap(self):
        with pytest.raises(HTTPException) as exc:
            gate_media_attachments(
                [self._info("video", duration=400.0)],
                self._settings(media_max_video_seconds=300.0),
            )
        assert exc.value.status_code == 400
        assert "时长超限" in exc.value.detail

    def test_duration_without_probe_metadata_skipped(self):
        gate_media_attachments([self._info("video", duration=None)], self._settings())


class TestVideoTranscode:
    """上传链路的非 mp4 视频统一转码（T-转码）。"""

    @staticmethod
    def _stub_registry(transcode_result):
        tool = _StubProbeTool(transcode_result)

        class _PathAwareProbe:
            def __init__(self):
                self.calls = []

            async def execute(self, *, on_progress=None, **kwargs):
                self.calls.append(kwargs)
                fmt = (
                    "mov,mp4,m4a,3gp,3g2,mj2"
                    if kwargs.get("file_path", "").endswith(".mp4")
                    else "matroska,webm"
                )
                return ToolResult(
                    success=True,
                    data={"success": True, "format_name": fmt, "duration_seconds": 2.0},
                )

        class _Registry:
            def __init__(self):
                self.probe = _PathAwareProbe()

            def get(self, name):
                if name == "probe_media":
                    return self.probe
                if name == "transcode_video":
                    return tool
                raise KeyError(name)

        registry = _Registry()
        return registry, tool

    def test_needs_transcode_decision(self):
        from courtier.agent.api.services.file_service import _needs_video_transcode

        assert _needs_video_transcode({"format_name": "matroska,webm"}) is True
        assert _needs_video_transcode({"format_name": "avi"}) is True
        assert _needs_video_transcode({"format_name": "mov,mp4,m4a,3gp,3g2,mj2"}) is False
        assert _needs_video_transcode({}) is True  # 未知格式宁转勿赌

    def test_webm_upload_transcoded_and_stored_as_mp4(self, tmp_path):
        import asyncio
        from pathlib import Path

        store = FileStore(str(tmp_path / "files"))
        registry, transcode_tool = self._stub_registry(
            ToolResult(success=True, data={"success": True, "minio_ref": "minio://bkt/out/rec.mp4"})
        )
        mp4_bytes = b"\x00\x00\x00\x18ftypisom" + b"x" * 64

        def fake_get_object(bucket, key):
            assert bucket == "bkt" and key == "out/rec.mp4"
            return mp4_bytes

        import courtier.storage.client as storage_client

        original = storage_client.get_object
        storage_client.get_object = fake_get_object
        try:
            info = asyncio.run(
                upload_file(
                    _upload_file(_wav_bytes(0.1), "recording.webm", "video/webm"),
                    SimpleNamespace(upload_dir=str(tmp_path / "uploads")),
                    store,
                    owner="u",
                    tool_registry=registry,
                )
            )
        finally:
            storage_client.get_object = original

        # probe 在转码后重跑：wav 工具桩返回的仍是 probe 结果，但存储文件已是 mp4
        assert info["fileId"]
        stored = asyncio.run(store.resolve(info["fileId"]))
        assert stored is not None
        assert stored.stored_path.endswith(".mp4")
        path = asyncio.run(store.resolve_path(info["fileId"], str(tmp_path / "uploads")))
        assert path.read_bytes() == mp4_bytes
        assert transcode_tool.calls[0]["file_path"].endswith(".webm")

    def test_transcode_failure_fails_closed(self, tmp_path):
        import asyncio

        registry, _ = self._stub_registry(ToolResult(success=False, error="ffmpeg boom"))
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                upload_file(
                    _upload_file(_wav_bytes(0.1), "recording.webm", "video/webm"),
                    SimpleNamespace(upload_dir=str(tmp_path / "uploads")),
                    FileStore(str(tmp_path / "files")),
                    owner="u",
                    tool_registry=registry,
                )
            )
        assert exc.value.status_code == 400
        assert "ffmpeg boom" in exc.value.detail

    def test_transcode_invalid_ref_fails_closed(self, tmp_path):
        import asyncio

        registry, _ = self._stub_registry(ToolResult(success=True, data={"success": True}))
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                upload_file(
                    _upload_file(_wav_bytes(0.1), "recording.webm", "video/webm"),
                    SimpleNamespace(upload_dir=str(tmp_path / "uploads")),
                    FileStore(str(tmp_path / "files")),
                    owner="u",
                    tool_registry=registry,
                )
            )
        assert exc.value.status_code == 400
