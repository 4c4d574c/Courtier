"""Test anydoc plugin — convert_document sandbox, format detection, error mapping."""

import sys
import types

import pytest

from .conftest import _ensure_plugin_path

_ensure_plugin_path("anydoc")

# Import at module level so the plugin's own ``tools`` module is cached
# in sys.modules before other plugin tests can shadow it.
from plugins.shared.anydoc.entry import AnyDocPlugin  # noqa: E402


def _fake_anydoc(
    *,
    detected: str | None = "docx",
    from_ext: str | None = None,
    markdown: str = "# 标题\n\n正文。",
) -> types.ModuleType:
    """Build a fake ``anydoc`` module matching the real API surface."""

    class ConvertError(Exception):
        pass

    class UnsupportedError(ConvertError):
        pass

    class EncryptedError(ConvertError):
        pass

    class MalformedError(ConvertError):
        pass

    mod = types.ModuleType("anydoc")
    mod.ConvertError = ConvertError
    mod.UnsupportedError = UnsupportedError
    mod.EncryptedError = EncryptedError
    mod.MalformedError = MalformedError
    mod.format_from_bytes = lambda data: detected
    mod.format_from_extension = lambda ext: from_ext
    mod.to_markdown_bytes = lambda data, fmt=None: markdown
    return mod


def _raising_anydoc(exc_name: str) -> types.ModuleType:
    """Fake ``anydoc`` whose conversion raises the named exception class."""

    mod = _fake_anydoc()
    exc_cls = getattr(mod, exc_name)

    def _raise(data, fmt=None):
        raise exc_cls("mock failure")

    mod.to_markdown_bytes = _raise
    return mod


def test_plugin_registers_with_system_prompt():
    plugin = AnyDocPlugin()
    caps = plugin.register_capabilities()
    assert "convert_document" in caps["system_prompt"]
    assert "OCR" in caps["system_prompt"]


class TestConvertDocumentSandbox:
    """Verify convert_document rejects paths outside the upload directory."""

    @pytest.mark.asyncio
    async def test_rejects_path_escape(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COURTIER_UPLOAD_DIR", str(tmp_path))
        import plugins.shared.anydoc.tools as tools_mod

        outside = tmp_path.parent / "secret.txt"
        outside.write_text("secret")
        tool = tools_mod.ConvertDocumentTool()
        result = await tool.execute(file_path=str(outside))
        assert result.success is False
        assert "Access denied" in result.error

    @pytest.mark.asyncio
    async def test_rejects_missing_upload_dir(self, monkeypatch):
        monkeypatch.delenv("COURTIER_UPLOAD_DIR", raising=False)
        monkeypatch.delenv("DOCAUDIT_UPLOAD_DIR", raising=False)
        monkeypatch.delenv("UPLOAD_DIR", raising=False)
        import plugins.shared.anydoc.tools as tools_mod

        tool = tools_mod.ConvertDocumentTool()
        result = await tool.execute(file_path="/tmp/test.docx")
        assert result.success is False
        assert "not configured" in result.error

    @pytest.mark.asyncio
    async def test_rejects_missing_file_path(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COURTIER_UPLOAD_DIR", str(tmp_path))
        import plugins.shared.anydoc.tools as tools_mod

        tool = tools_mod.ConvertDocumentTool()
        result = await tool.execute()
        assert result.success is False
        assert "file_path" in result.error


class TestConvertDocument:
    @pytest.mark.asyncio
    async def test_success(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COURTIER_UPLOAD_DIR", str(tmp_path))
        monkeypatch.setitem(sys.modules, "anydoc", _fake_anydoc())
        import plugins.shared.anydoc.tools as tools_mod

        doc = tmp_path / "notice.docx"
        doc.write_bytes(b"fake docx bytes")
        tool = tools_mod.ConvertDocumentTool()
        result = await tool.execute(file_path=str(doc))
        assert result.success is True
        assert result.data["markdown"] == "# 标题\n\n正文。"
        assert result.data["format"] == "docx"

    @pytest.mark.asyncio
    async def test_csv_falls_back_to_extension(self, tmp_path, monkeypatch):
        """CSV has no content marker: detection returns None, extension names it."""
        monkeypatch.setenv("COURTIER_UPLOAD_DIR", str(tmp_path))
        monkeypatch.setitem(
            sys.modules, "anydoc", _fake_anydoc(detected=None, from_ext="csv", markdown="| a | b |")
        )
        import plugins.shared.anydoc.tools as tools_mod

        doc = tmp_path / "data.csv"
        doc.write_text("a,b\n1,2\n")
        tool = tools_mod.ConvertDocumentTool()
        result = await tool.execute(file_path=str(doc))
        assert result.success is True
        assert result.data["format"] == "csv"

    @pytest.mark.asyncio
    async def test_unknown_format_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COURTIER_UPLOAD_DIR", str(tmp_path))
        monkeypatch.setitem(sys.modules, "anydoc", _fake_anydoc(detected=None, from_ext=None))
        import plugins.shared.anydoc.tools as tools_mod

        doc = tmp_path / "mystery.bin"
        doc.write_bytes(b"\x00\x01")
        tool = tools_mod.ConvertDocumentTool()
        result = await tool.execute(file_path=str(doc))
        assert result.success is False
        assert "无法识别" in result.error

    @pytest.mark.asyncio
    async def test_unsupported_points_to_parse_document(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COURTIER_UPLOAD_DIR", str(tmp_path))
        monkeypatch.setitem(sys.modules, "anydoc", _raising_anydoc("UnsupportedError"))
        import plugins.shared.anydoc.tools as tools_mod

        doc = tmp_path / "scanned.pdf"
        doc.write_bytes(b"%PDF-1.4 fake")
        tool = tools_mod.ConvertDocumentTool()
        result = await tool.execute(file_path=str(doc))
        assert result.success is False
        assert "parse_document" in result.error

    @pytest.mark.asyncio
    async def test_encrypted_error(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COURTIER_UPLOAD_DIR", str(tmp_path))
        monkeypatch.setitem(sys.modules, "anydoc", _raising_anydoc("EncryptedError"))
        import plugins.shared.anydoc.tools as tools_mod

        doc = tmp_path / "locked.docx"
        doc.write_bytes(b"fake")
        tool = tools_mod.ConvertDocumentTool()
        result = await tool.execute(file_path=str(doc))
        assert result.success is False
        assert "加密" in result.error

    @pytest.mark.asyncio
    async def test_malformed_error(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COURTIER_UPLOAD_DIR", str(tmp_path))
        monkeypatch.setitem(sys.modules, "anydoc", _raising_anydoc("MalformedError"))
        import plugins.shared.anydoc.tools as tools_mod

        doc = tmp_path / "broken.docx"
        doc.write_bytes(b"fake")
        tool = tools_mod.ConvertDocumentTool()
        result = await tool.execute(file_path=str(doc))
        assert result.success is False
        assert "无法转换" in result.error
