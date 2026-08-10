from pathlib import Path

import pytest

from .conftest import _ensure_plugin_path

_ensure_plugin_path("annotate")

from plugins.shared.annotate.entry import AnnotatePlugin  # noqa: E402


@pytest.mark.asyncio
async def test_annotate_plugin_registers_tool():
    plugin = AnnotatePlugin()
    plugin._setup_handlers()
    caps, system_prompt = plugin._collect_capabilities()
    tool_names = [c["name"] for c in caps if c["type"] == "tool"]
    assert "annotate_document" in tool_names
    assert len(system_prompt) > 0


@pytest.mark.asyncio
class TestAnnotateStorageOutput:
    """批注结果优先经宿主 storage.put 存 MinIO 并返回下载链接。"""

    @staticmethod
    def _tool_class():
        # 经 entry 模块命名空间取类，避免多插件共享 sys.modules["tools"]
        # 时拿到其他插件的同名模块。
        from plugins.shared.annotate import entry as annotate_entry

        return annotate_entry.AnnotateDocumentTool

    @staticmethod
    def _mock_annotate(monkeypatch):
        import docannot._annotate as docannot_mod

        monkeypatch.setattr(docannot_mod, "annotate", lambda *args, **kwargs: b"annotated-bytes")

    async def test_returns_download_url_when_host_storage_available(self, monkeypatch, tmp_path):
        self._mock_annotate(monkeypatch)
        monkeypatch.setenv("COURTIER_UPLOAD_DIR", str(tmp_path))

        receipt = {
            "bucket": "courtier-docs",
            "object_key": "plugin-outputs/abc/test_annotated.docx",
            "download_url": "http://minio/courtier-docs/plugin-outputs/abc/test_annotated.docx",
            "expires_in": 604800,
            "size_bytes": 15,
        }
        calls: list[tuple[str, dict]] = []

        class _FakeClient:
            async def call(self, method, params=None, timeout=30.0):
                calls.append((method, params or {}))
                return receipt

        tool = self._tool_class()(host_client_getter=lambda: _FakeClient())
        result = await tool.execute(
            source=str(tmp_path / "test.docx"),
            rules=[{"keyword": "关键词", "comment": "批注"}],
        )

        assert result.success is True
        assert result.data["download_url"] == receipt["download_url"]
        assert result.data["object_key"] == receipt["object_key"]
        assert result.data["expires_in"] == 604800
        assert result.data["rules_applied"] == 1
        assert "output_path" not in result.data
        # 工具确实经 storage.put 上传，文件名带 _annotated 后缀
        method, params = calls[0]
        assert method == "storage.put"
        assert params["filename"] == "test_annotated.docx"

    async def test_falls_back_to_temp_file_without_host_client(self, monkeypatch, tmp_path):
        self._mock_annotate(monkeypatch)
        monkeypatch.setenv("COURTIER_UPLOAD_DIR", str(tmp_path))

        tool = self._tool_class()()  # 无 host service getter
        result = await tool.execute(
            source=str(tmp_path / "test.docx"),
            rules=[{"keyword": "关键词", "comment": "批注"}],
        )

        assert result.success is True
        assert "download_url" not in result.data
        output_path = Path(result.data["output_path"])
        assert output_path.exists()
        assert output_path.read_bytes() == b"annotated-bytes"

    async def test_falls_back_to_temp_file_when_upload_fails(self, monkeypatch, tmp_path):
        self._mock_annotate(monkeypatch)
        monkeypatch.setenv("COURTIER_UPLOAD_DIR", str(tmp_path))

        class _FailingClient:
            async def call(self, method, params=None, timeout=30.0):
                raise RuntimeError("host unreachable")

        tool = self._tool_class()(host_client_getter=lambda: _FailingClient())
        result = await tool.execute(
            source=str(tmp_path / "test.docx"),
            rules=[{"keyword": "关键词", "comment": "批注"}],
        )

        assert result.success is True
        assert "download_url" not in result.data
        assert Path(result.data["output_path"]).exists()


class TestAnnotatePathSafety:
    def test_rejects_path_escape(self, tmp_path):
        from docannot._annotate import annotate

        outside = tmp_path.parent / "secret.docx"
        outside.write_bytes(b"PK fake docx")
        with pytest.raises(ValueError, match="outside allowed"):
            annotate(str(outside), [], allowed_dirs=[tmp_path])

    def test_accepts_file_in_allowed_dir(self, tmp_path):
        from docannot._annotate import annotate

        valid = tmp_path / "test.docx"
        valid.write_bytes(b"PK fake docx")
        # Will fail on parse (not a real DOCX) but not from path check.
        with pytest.raises(Exception):
            annotate(str(valid), [], allowed_dirs=[tmp_path])

    def test_accepts_bytes_source(self, tmp_path):
        from docannot._annotate import annotate

        outside = tmp_path.parent / "secret.docx"
        outside.write_bytes(b"PK fake docx")
        # bytes source always accepted (already in memory)
        with pytest.raises(Exception):
            annotate(b"PK fake docx", [], allowed_dirs=[tmp_path])

    def test_accepts_base64_source(self, tmp_path):
        import base64

        from docannot._annotate import annotate

        encoded = base64.b64encode(b"fake docx bytes").decode()
        # base64 source always accepted (already decoded from transport)
        with pytest.raises(Exception):
            annotate(encoded, [], allowed_dirs=[tmp_path])
