"""Tests for ProxyTool file-ref rewriting (upload-dir path → minio:// ref)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from courtier.plugin.proxies import ProxyTool

BUCKET = "courtier-plugin-io"


class _FakeClient:
    plugin_name = "anydoc"

    def __init__(self):
        self.calls: list[dict] = []

    async def call(self, method, params=None, timeout=30.0):
        self.calls.append({"method": method, "params": params})
        return {"success": True, "data": {"ok": True}}


@pytest.fixture
def minio_spy(monkeypatch):
    """Fake the storage client surface used by the rewrite path."""
    state = {"objects": {}, "puts": []}

    def _object_exists(bucket: str, key: str) -> bool:
        return (bucket, key) in state["objects"]

    def _fput_object(bucket: str, key: str, path: str, content_type: str) -> None:
        state["objects"][(bucket, key)] = Path(path).read_bytes()
        state["puts"].append((bucket, key, content_type))

    monkeypatch.setattr("courtier.storage.client.object_exists", _object_exists)
    monkeypatch.setattr("courtier.storage.client.fput_object", _fput_object)
    return state


def _tool_spec(*, file_params: tuple[str, ...] = ("file_path",)) -> dict:
    properties = {
        "file_path": {"type": "string", "description": "path"},
        "note": {"type": "string", "description": "plain string"},
    }
    for name in file_params:
        properties[name] = {**properties.get(name, {"type": "string"}), "format": "file-ref"}
    return {"name": "convert_document", "parameters": {"type": "object", "properties": properties}}


def _patch_settings(monkeypatch, upload_dir: Path):
    fake = SimpleNamespace(upload_dir=str(upload_dir), minio_bucket_plugin_io=BUCKET)
    monkeypatch.setattr("courtier.config.get_settings", lambda: fake)


class TestFileRefRewrite:
    @pytest.mark.asyncio
    async def test_path_inside_upload_dir_rewritten(
        self, tmp_path, monkeypatch, minio_spy
    ):
        upload = tmp_path / "uploads"
        doc = upload / "sess1" / "通知.docx"
        doc.parent.mkdir(parents=True)
        doc.write_bytes(b"docx-bytes")
        _patch_settings(monkeypatch, upload)

        client = _FakeClient()
        proxy = ProxyTool(client, _tool_spec())
        result = await proxy.execute(on_progress=lambda *_: None, file_path=str(doc))

        assert result.success is True
        sent = client.calls[0]["params"]["args"]["file_path"]
        assert sent.startswith(f"minio://{BUCKET}/in/")
        assert sent.endswith("/通知.docx")
        # The file was uploaded once.
        assert len(minio_spy["puts"]) == 1

    @pytest.mark.asyncio
    async def test_same_file_not_reuploaded(self, tmp_path, monkeypatch, minio_spy):
        upload = tmp_path / "uploads"
        doc = upload / "report.docx"
        upload.mkdir(parents=True)
        doc.write_bytes(b"same")
        _patch_settings(monkeypatch, upload)

        client = _FakeClient()
        proxy = ProxyTool(client, _tool_spec())
        await proxy.execute(on_progress=lambda *_: None, file_path=str(doc))
        ref1 = client.calls[0]["params"]["args"]["file_path"]
        await proxy.execute(on_progress=lambda *_: None, file_path=str(doc))
        ref2 = client.calls[1]["params"]["args"]["file_path"]

        assert ref1 == ref2
        assert len(minio_spy["puts"]) == 1  # second dispatch hit the existence cache

    @pytest.mark.asyncio
    async def test_path_escape_is_denied(self, tmp_path, monkeypatch, minio_spy):
        upload = tmp_path / "uploads"
        upload.mkdir()
        outside = tmp_path / "secret.txt"
        outside.write_text("secret")
        _patch_settings(monkeypatch, upload)

        client = _FakeClient()
        proxy = ProxyTool(client, _tool_spec())
        result = await proxy.execute(on_progress=lambda *_: None, file_path=str(outside))

        assert result.success is False
        assert "越出上传目录" in result.error
        assert client.calls == []  # never dispatched
        assert minio_spy["puts"] == []

    @pytest.mark.asyncio
    async def test_missing_file_is_denied(self, tmp_path, monkeypatch, minio_spy):
        upload = tmp_path / "uploads"
        upload.mkdir()
        _patch_settings(monkeypatch, upload)

        client = _FakeClient()
        proxy = ProxyTool(client, _tool_spec())
        result = await proxy.execute(
            on_progress=lambda *_: None, file_path=str(upload / "ghost.docx")
        )
        assert result.success is False
        assert "不存在" in result.error
        assert client.calls == []

    @pytest.mark.asyncio
    async def test_base64_value_passes_through(self, tmp_path, monkeypatch, minio_spy):
        """annotate's dual-mode `source`: inline base64 is data, not a path."""
        import base64

        upload = tmp_path / "uploads"
        upload.mkdir()
        _patch_settings(monkeypatch, upload)

        client = _FakeClient()
        proxy = ProxyTool(client, _tool_spec(file_params=("source",)))
        payload = base64.b64encode(b"fake-docx-bytes" * 64).decode()  # legit base64
        result = await proxy.execute(on_progress=lambda *_: None, source=payload)

        assert result.success is True
        assert client.calls[0]["params"]["args"]["source"] == payload
        assert minio_spy["puts"] == []

    @pytest.mark.asyncio
    async def test_unmarked_params_untouched(self, tmp_path, monkeypatch, minio_spy):
        upload = tmp_path / "uploads"
        upload.mkdir()
        _patch_settings(monkeypatch, upload)

        client = _FakeClient()
        proxy = ProxyTool(client, _tool_spec())
        result = await proxy.execute(
            on_progress=lambda *_: None, note="/uploads/looks/like/a/path.docx"
        )
        assert result.success is True
        assert client.calls[0]["params"]["args"]["note"] == "/uploads/looks/like/a/path.docx"

    @pytest.mark.asyncio
    async def test_no_file_ref_params_short_circuits(self):
        client = _FakeClient()
        proxy = ProxyTool(client, {"name": "check_content", "parameters": {}})
        result = await proxy.execute(on_progress=lambda *_: None, text="正文")
        assert result.success is True
        assert client.calls[0]["params"]["args"] == {"text": "正文"}


def test_resolve_file_rejects_foreign_bucket(monkeypatch, tmp_path):
    """Defense in depth: resolve_file refuses references outside the
    transfer bucket even if IAM were misconfigured."""
    import asyncio

    from courtier_plugin_sdk import files as sdk_files

    monkeypatch.setattr(sdk_files, "_base_dir", lambda: tmp_path)
    with pytest.raises(ValueError, match="not the transfer bucket"):
        asyncio.run(sdk_files.resolve_file("minio://courtier-docs/some/obj.bin"))
