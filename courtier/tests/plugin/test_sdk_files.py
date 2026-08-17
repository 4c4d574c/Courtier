"""Tests for the SDK file-transfer helpers (resolve_file / put_file / workdir)."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from courtier_plugin_sdk import files
from courtier_plugin_sdk.files import put_file, request_workdir, resolve_file

BUCKET = "courtier-plugin-io"


class _FakeResponse:
    """Mimic urllib3 response surface used by files._download."""

    def __init__(self, data: bytes):
        self._buf = io.BytesIO(data)
        self.closed = False
        self.released = False

    def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)

    def close(self):
        self.closed = True

    def release_conn(self):
        self.released = True


class _FakeMinio:
    def __init__(self, objects: dict[tuple[str, str], bytes] | None = None):
        self.objects = objects or {}
        self.uploads: list[tuple[str, str, str, str]] = []

    def get_object(self, bucket: str, key: str):
        if (bucket, key) not in self.objects:
            raise FileNotFoundError(key)
        return _FakeResponse(self.objects[(bucket, key)])

    def fput_object(self, bucket: str, key: str, path: str, content_type: str):
        self.uploads.append((bucket, key, path, content_type))
        self.objects[(bucket, key)] = Path(path).read_bytes()


@pytest.fixture
def fake_minio(monkeypatch):
    client = _FakeMinio({(BUCKET, "in/abc/report.docx"): b"docx-bytes"})
    monkeypatch.setattr(files, "_client", client)
    monkeypatch.setenv("MINIO_ENDPOINT", "minio:9000")
    monkeypatch.setenv("MINIO_ACCESS_KEY", "ak")
    monkeypatch.setenv("MINIO_SECRET_KEY", "sk")
    monkeypatch.delenv(files.ENV_WORKDIR, raising=False)
    monkeypatch.setattr(files, "_base_workdir", None)
    return client


class TestResolveFile:
    @pytest.mark.asyncio
    async def test_plain_path_passes_through(self):
        assert await resolve_file("/tmp/whatever.docx") == "/tmp/whatever.docx"

    @pytest.mark.asyncio
    async def test_minio_ref_downloads_preserving_filename(self, fake_minio, tmp_path):
        async with request_workdir() as workdir:
            local = await resolve_file(f"minio://{BUCKET}/in/abc/report.docx")
            assert Path(local).parent == workdir
            assert Path(local).name == "report.docx"
            assert Path(local).read_bytes() == b"docx-bytes"
        # Request workdir is swept after the dispatch.
        assert not workdir.exists()

    @pytest.mark.asyncio
    async def test_filename_collision_gets_counter_prefix(self, fake_minio):
        fake_minio.objects[(BUCKET, "in/def/report.docx")] = b"other"
        async with request_workdir():
            first = await resolve_file(f"minio://{BUCKET}/in/abc/report.docx")
            second = await resolve_file(f"minio://{BUCKET}/in/def/report.docx")
            assert Path(first).name == "report.docx"
            assert Path(second).name == "1-report.docx"

    @pytest.mark.asyncio
    async def test_invalid_ref_raises(self):
        with pytest.raises(ValueError):
            await resolve_file("minio://bucket-only")

    @pytest.mark.asyncio
    async def test_missing_env_lists_variables(self, monkeypatch):
        monkeypatch.setattr(files, "_client", None)
        for var in ("MINIO_ENDPOINT", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY"):
            monkeypatch.delenv(var, raising=False)
        with pytest.raises(RuntimeError, match="MINIO_ENDPOINT"):
            await resolve_file(f"minio://{BUCKET}/in/abc/report.docx")


class TestPutFile:
    @pytest.mark.asyncio
    async def test_uploads_and_returns_ref(self, fake_minio, tmp_path):
        files.configure(plugin_name="annotate")
        src = tmp_path / "out.docx"
        src.write_bytes(b"annotated")
        ref = await put_file(src)
        assert ref.startswith(f"minio://{BUCKET}/out/annotate/")
        assert ref.endswith("/out.docx")
        bucket, key, path, ctype = fake_minio.uploads[0]
        assert bucket == BUCKET
        assert fake_minio.objects[(bucket, key)] == b"annotated"
        assert ctype.endswith("wordprocessingml.document")

    @pytest.mark.asyncio
    async def test_missing_file_raises(self):
        with pytest.raises(FileNotFoundError):
            await put_file("/nonexistent/nothing.docx")


class TestToolExecuteWorkdir:
    @pytest.mark.asyncio
    async def test_default_dispatcher_wraps_request_workdir(self, fake_minio):
        from courtier_plugin_sdk import PluginRuntime, ToolResult

        seen: dict[str, str] = {}

        class FetchTool:
            name = "fetch"
            description = "download and report location"
            parameters = {"type": "object", "properties": {}}

            async def execute(self, file_path: str = ""):
                local = await resolve_file(file_path)
                seen["local"] = local
                seen["exists_during"] = str(Path(local).exists())
                return ToolResult(success=True, data={"local": local})

        runtime = PluginRuntime()
        runtime.register_tool(FetchTool())
        result = await runtime._default_tool_execute(
            {"tool": "fetch", "args": {"file_path": f"minio://{BUCKET}/in/abc/report.docx"}}
        )
        assert result["success"] is True
        assert seen["exists_during"] == "True"
        # Per-request dir is cleaned after dispatch.
        assert not Path(seen["local"]).parent.exists()
