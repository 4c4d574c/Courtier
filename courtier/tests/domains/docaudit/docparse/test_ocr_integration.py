"""PaddleOCR / PPStructureV3 integration tests for the scanned pipeline.

Two layers:

- Offline error-path tests (run by default): verify the pipeline fails
  honestly when the OCR service is unreachable — no silent empty documents.
- End-to-end tests (``pytest.mark.integration``): require a live
  PPStructureV3 OCR service.  See the class docstring for the required
  environment variables.
"""

from __future__ import annotations

import os
import socket

import pytest
from docparse.parsers.base import ParserConfig
from docparse.parsers.ocr.base import OCRPageResult
from docparse.parsers.ocr.ppstructure import PPStructureAdapter
from docparse.parsers.scanned import ScannedParser
from PIL import Image as PILImage


def _unused_port() -> int:
    """Return a localhost port that is (almost certainly) not listened on."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _write_png(path, size=(400, 600)) -> str:
    PILImage.new("RGB", size, 255).save(str(path))
    return str(path)


class TestOcrUnreachableOffline:
    """Offline error paths — no OCR service needed, always runnable."""

    def test_ppstructure_unreachable_raises_runtime_error(self, tmp_path):
        """A dead OCR endpoint surfaces as RuntimeError, not an empty page."""
        img = _write_png(tmp_path / "page.png")
        adapter = PPStructureAdapter(api_url=f"http://127.0.0.1:{_unused_port()}/ocr")
        with pytest.raises(RuntimeError, match="PPStructureV3 API call failed"):
            adapter.recognize(img)

    def test_scanned_parser_fails_when_ocr_unreachable(self, tmp_path):
        """ScannedParser must raise (not return a blank Document) when OCR
        fails for every page."""
        img = _write_png(tmp_path / "page.png")
        config = ParserConfig(
            ocr_api_url=f"http://127.0.0.1:{_unused_port()}/ocr",
        )
        with pytest.raises(RuntimeError, match="OCR 识别均失败"):
            ScannedParser().parse(img, config)


@pytest.mark.integration
class TestPPStructureLiveIntegration:
    """End-to-end tests against a live PPStructureV3 OCR service.

    Required environment:
    - ``DOCPARSE_OCR_API_URL``: endpoint of a running PPStructureV3
      service (e.g. ``http://localhost:8006/ocr``).

    Run with: ``uv run pytest tests/domains/docaudit/docparse -m integration``
    """

    def test_recognize_returns_structured_result(self, tmp_path):
        """A live OCR service answers with a parseable OCRPageResult."""
        api_url = os.getenv("DOCPARSE_OCR_API_URL")
        if not api_url:
            pytest.skip("DOCPARSE_OCR_API_URL not set — no live OCR service")

        img = _write_png(tmp_path / "page.png")
        result = PPStructureAdapter(api_url=api_url).recognize(img)
        assert isinstance(result, OCRPageResult)

    def test_full_scanned_pipeline(self, tmp_path):
        """Full image → Document pipeline (OCR + rule engine) with a live OCR service."""
        api_url = os.getenv("DOCPARSE_OCR_API_URL")
        if not api_url:
            pytest.skip("Requires DOCPARSE_OCR_API_URL pointing at a live OCR service")

        img = _write_png(tmp_path / "page.png")
        doc = ScannedParser().parse(img, ParserConfig.from_env())
        assert doc.total_page_num == 1
        assert len(doc.pages) == 1
