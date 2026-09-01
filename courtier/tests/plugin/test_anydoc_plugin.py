"""Test anydoc plugin — convert_document sandbox, format detection, error mapping."""

import sys
import types
from pathlib import Path

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
    """Path handling after the sandbox moved to the host proxy boundary.

    The host enforces the upload-dir sandbox before rewriting file
    arguments into minio:// references; the plugin only ever sees those
    references (production) or plain local paths (tests / same-machine
    development), which pass through resolve_file unchanged.
    """

    @pytest.mark.asyncio
    async def test_missing_file_is_rejected(self, tmp_path):
        import plugins.shared.anydoc.tools as tools_mod

        tool = tools_mod.ConvertDocumentTool()
        result = await tool.execute(file_path=str(tmp_path / "nope.docx"))
        assert result.success is False
        assert "File not found" in result.error

    @pytest.mark.asyncio
    async def test_existing_local_file_passes_through(self, tmp_path, monkeypatch):
        """Local files are read regardless of any upload-dir configuration."""
        monkeypatch.delenv("COURTIER_UPLOAD_DIR", raising=False)
        monkeypatch.delenv("DOCAUDIT_UPLOAD_DIR", raising=False)
        monkeypatch.delenv("UPLOAD_DIR", raising=False)
        monkeypatch.setitem(sys.modules, "anydoc", _fake_anydoc())
        import plugins.shared.anydoc.tools as tools_mod

        doc = tmp_path / "notice.docx"
        doc.write_bytes(b"fake docx bytes")
        tool = tools_mod.ConvertDocumentTool()
        result = await tool.execute(file_path=str(doc))
        assert result.success is True
        assert result.data["markdown"] == "# 标题\n\n正文。"

    @pytest.mark.asyncio
    async def test_rejects_missing_file_path(self):
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
    async def test_unsupported_points_to_parse_layout(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COURTIER_UPLOAD_DIR", str(tmp_path))
        # Without an OCR endpoint configured the fallback is disabled, so the
        # error keeps pointing at parse_layout.
        monkeypatch.delenv("ANYDOC_OCR_API_URL", raising=False)
        monkeypatch.setitem(sys.modules, "anydoc", _raising_anydoc("UnsupportedError"))
        import plugins.shared.anydoc.tools as tools_mod

        doc = tmp_path / "scanned.pdf"
        doc.write_bytes(b"%PDF-1.4 fake")
        tool = tools_mod.ConvertDocumentTool()
        result = await tool.execute(file_path=str(doc))
        assert result.success is False
        assert "parse_layout" in result.error

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


class _FakeResponse:
    """Minimal requests.post response stub with a JSON payload."""

    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def _fake_fitz(page_count: int) -> types.ModuleType:
    """Build a fake ``fitz`` module rendering ``page_count`` in-memory pages."""

    class _Pix:
        def __init__(self, page_no: int) -> None:
            self._page_no = page_no

        def save(self, path: str) -> None:
            Path(path).write_bytes(f"page-{self._page_no}".encode())

    class _Page:
        def __init__(self, page_no: int) -> None:
            self._page_no = page_no

        def get_pixmap(self, dpi: int = 150) -> _Pix:
            return _Pix(self._page_no)

    class _Doc:
        def __init__(self, _path: str) -> None:
            self._pages = [_Page(i) for i in range(page_count)]

        def __len__(self) -> int:
            return len(self._pages)

        def __getitem__(self, idx: int) -> _Page:
            return self._pages[idx]

        def close(self) -> None:
            pass

    mod = types.ModuleType("fitz")
    mod.open = staticmethod(lambda path: _Doc(str(path)))
    return mod


class TestConvertDocumentOCR:
    """OCR fallback path: images and scanned PDFs → PPOCR /ocr/text."""

    @pytest.mark.asyncio
    async def test_image_goes_directly_to_ocr(self, tmp_path, monkeypatch):
        """Image extensions skip anydoc entirely and hit the OCR endpoint."""
        monkeypatch.setenv("COURTIER_UPLOAD_DIR", str(tmp_path))
        monkeypatch.setenv("ANYDOC_OCR_API_URL", "http://ocr.example/ocr/text")
        import plugins.shared.anydoc.ocr as ocr_mod

        seen: dict = {}

        def fake_post(url, files=None, timeout=None):
            seen["url"] = url
            seen["filename"] = files["file"][0]
            seen["mime"] = files["file"][2]
            return _FakeResponse({"markdown": "扫描图片内容"})

        monkeypatch.setattr(ocr_mod.requests, "post", fake_post)
        import plugins.shared.anydoc.tools as tools_mod

        img = tmp_path / "scan.jpg"
        img.write_bytes(b"fake jpeg")
        tool = tools_mod.ConvertDocumentTool()
        result = await tool.execute(file_path=str(img))
        assert result.success is True
        assert result.data["markdown"] == "扫描图片内容"
        assert result.data["ocr"] is True
        assert result.data["pages"] == 1
        assert result.data["format"] == "jpg"
        assert seen["url"] == "http://ocr.example/ocr/text"
        assert seen["filename"] == "scan.jpg"
        assert seen["mime"] == "image/jpeg"

    @pytest.mark.asyncio
    async def test_image_unconfigured_points_to_parse_layout(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COURTIER_UPLOAD_DIR", str(tmp_path))
        monkeypatch.delenv("ANYDOC_OCR_API_URL", raising=False)
        import plugins.shared.anydoc.tools as tools_mod

        img = tmp_path / "scan.png"
        img.write_bytes(b"fake png")
        tool = tools_mod.ConvertDocumentTool()
        result = await tool.execute(file_path=str(img))
        assert result.success is False
        assert "parse_layout" in result.error

    @pytest.mark.asyncio
    async def test_scanned_pdf_uses_ocr_path(self, tmp_path, monkeypatch):
        """anydoc.UnsupportedError on a PDF falls back to per-page OCR."""
        monkeypatch.setenv("COURTIER_UPLOAD_DIR", str(tmp_path))
        monkeypatch.setenv("ANYDOC_OCR_API_URL", "http://ocr.example/ocr/text")
        monkeypatch.setitem(sys.modules, "anydoc", _raising_anydoc("UnsupportedError"))
        monkeypatch.setitem(sys.modules, "fitz", _fake_fitz(2))
        import plugins.shared.anydoc.ocr as ocr_mod

        calls: list[str] = []

        def fake_post(url, files=None, timeout=None):
            calls.append(files["file"][0])
            return _FakeResponse({"markdown": f"内容{len(calls)}"})

        monkeypatch.setattr(ocr_mod.requests, "post", fake_post)
        import plugins.shared.anydoc.tools as tools_mod

        pdf = tmp_path / "scanned.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        tool = tools_mod.ConvertDocumentTool()
        result = await tool.execute(file_path=str(pdf))
        assert result.success is True
        assert result.data["ocr"] is True
        assert result.data["pages"] == 2
        assert result.data["format"] == "pdf"
        md = result.data["markdown"]
        assert md.index("【第 1 页】") < md.index("【第 2 页】")
        assert "内容1" in md and "内容2" in md

    @pytest.mark.asyncio
    async def test_scanned_pdf_unconfigured_points_to_parse_layout(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COURTIER_UPLOAD_DIR", str(tmp_path))
        monkeypatch.delenv("ANYDOC_OCR_API_URL", raising=False)
        monkeypatch.setitem(sys.modules, "anydoc", _raising_anydoc("UnsupportedError"))
        import plugins.shared.anydoc.tools as tools_mod

        pdf = tmp_path / "scanned.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        tool = tools_mod.ConvertDocumentTool()
        result = await tool.execute(file_path=str(pdf))
        assert result.success is False
        assert "parse_layout" in result.error

    @pytest.mark.asyncio
    async def test_ocr_failure_reports_page_number(self, tmp_path, monkeypatch):
        """A failing page fails the whole job and names the failing page."""
        monkeypatch.setenv("COURTIER_UPLOAD_DIR", str(tmp_path))
        monkeypatch.setenv("ANYDOC_OCR_API_URL", "http://ocr.example/ocr/text")
        monkeypatch.setitem(sys.modules, "anydoc", _raising_anydoc("UnsupportedError"))
        monkeypatch.setitem(sys.modules, "fitz", _fake_fitz(3))
        import requests

        import plugins.shared.anydoc.ocr as ocr_mod

        def fake_post(url, files=None, timeout=None):
            raise requests.ConnectionError("service down")

        monkeypatch.setattr(ocr_mod.requests, "post", fake_post)
        import plugins.shared.anydoc.tools as tools_mod

        pdf = tmp_path / "scanned.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        tool = tools_mod.ConvertDocumentTool()
        result = await tool.execute(file_path=str(pdf))
        assert result.success is False
        assert "第 1 页" in result.error


class TestEmptyExtraction:
    @pytest.mark.asyncio
    async def test_empty_markdown_fails_with_guidance(self, tmp_path, monkeypatch):
        """A document that yields zero text (empty shell docx, pure-image
        content) must fail loudly — a silent "" markdown sends the model
        chasing a nonexistent full version via get_artifact."""
        monkeypatch.setenv("COURTIER_UPLOAD_DIR", str(tmp_path))
        monkeypatch.setitem(sys.modules, "anydoc", _fake_anydoc(markdown=""))
        import plugins.shared.anydoc.tools as tools_mod

        doc = tmp_path / "empty.docx"
        doc.write_bytes(b"fake empty docx")
        result = await tools_mod.ConvertDocumentTool().execute(file_path=str(doc))
        assert result.success is False
        assert "未提取到任何文本内容" in result.error
        assert "parse_layout" in result.error

    @pytest.mark.asyncio
    async def test_whitespace_only_markdown_also_fails(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COURTIER_UPLOAD_DIR", str(tmp_path))
        monkeypatch.setitem(sys.modules, "anydoc", _fake_anydoc(markdown="  \n\n \t"))
        import plugins.shared.anydoc.tools as tools_mod

        doc = tmp_path / "blank.docx"
        doc.write_bytes(b"fake blank docx")
        result = await tools_mod.ConvertDocumentTool().execute(file_path=str(doc))
        assert result.success is False
