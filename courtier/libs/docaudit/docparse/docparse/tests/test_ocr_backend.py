"""Tests for OCR backend abstraction layer."""

import pytest
from docparse.parsers.ocr.base import OCRBlock, OCRLineResult, OCRPageResult
from docparse.parsers.ocr.factory import create_ocr_engine
from docparse.parsers.ocr.ppstructure import PPStructureAdapter

SAMPLE_API_RESPONSE = {
    "results": [
        {
            "rec_texts": ["软件开发模式：", "软件开发模式是指..."],
            "rec_scores": [0.98, 0.97],
            "rec_boxes": [[20, 17, 352, 76], [22, 196, 1364, 235]],
            "rec_polys": [
                [[20, 17], [352, 17], [352, 76], [20, 76]],
                [[22, 196], [1364, 197], [1364, 235], [22, 234]],
            ],
            "text_word": [
                ["软件", "开发", "模式："],
                ["软件", "开发", "模式", "是指", "..."],
            ],
            "text_word_boxes": [
                [[20, 17, 120, 76], [121, 17, 280, 76], [281, 17, 352, 76]],
                [
                    [22, 196, 122, 235],
                    [123, 196, 423, 235],
                    [424, 196, 624, 235],
                    [625, 196, 925, 235],
                    [926, 196, 1364, 235],
                ],
            ],
        }
    ]
}


class TestOCRLineResult:
    """Test OCRLineResult data model."""

    def test_creation_with_required_fields(self):
        line = OCRLineResult(
            text="Hello",
            line_no=0,
            x0=10.0,
            y0=20.0,
            x1=100.0,
            y1=40.0,
        )
        assert line.text == "Hello"
        assert line.line_no == 0
        assert line.x0 == 10.0
        assert line.y0 == 20.0
        assert line.x1 == 100.0
        assert line.y1 == 40.0
        assert line.polys == []
        assert line.confidence == 0.0
        assert line.chars == []

    def test_creation_with_polys(self):
        polys = [(10.0, 20.0), (100.0, 20.0), (100.0, 40.0), (10.0, 40.0)]
        line = OCRLineResult(
            text="Test",
            line_no=1,
            x0=10.0,
            y0=20.0,
            x1=100.0,
            y1=40.0,
            polys=polys,
            confidence=0.95,
        )
        assert len(line.polys) == 4
        assert line.confidence == 0.95


class TestOCRPageResult:
    """Test OCRPageResult data model."""

    def test_creation(self):
        lines = [
            OCRLineResult(
                text="Line 1",
                line_no=0,
                x0=10.0,
                y0=20.0,
                x1=100.0,
                y1=40.0,
            ),
        ]
        blocks = [
            OCRBlock(label="text", x0=10.0, y0=20.0, x1=100.0, y1=40.0),
        ]
        result = OCRPageResult(
            lines=lines,
            width=1472,
            height=943,
            blocks=blocks,
        )
        assert len(result.lines) == 1
        assert result.width == 1472
        assert result.height == 943
        assert len(result.blocks) == 1
        assert result.raw == {}

    def test_default_empty_blocks_and_raw(self):
        result = OCRPageResult(lines=[], width=800, height=600)
        assert result.blocks == []
        assert result.raw == {}


class TestPPStructureAdapter:
    """Test PPStructureAdapter OCR engine."""

    def test_parse_api_response(self):
        adapter = PPStructureAdapter(api_url="http://localhost:8006/ocr")
        page_data = SAMPLE_API_RESPONSE["results"][0]
        result = adapter.parse_response(page_data)

        assert isinstance(result, OCRPageResult)
        assert result.width == 1364
        assert result.height == 235
        assert len(result.lines) == 2

    def test_parsed_line_content(self):
        adapter = PPStructureAdapter(api_url="http://localhost:8006/ocr")
        page_data = SAMPLE_API_RESPONSE["results"][0]
        result = adapter.parse_response(page_data)

        first_line = result.lines[0]
        assert first_line.text == "软件开发模式："
        assert first_line.line_no == 0
        assert first_line.confidence == 0.98
        assert first_line.x0 == 20.0
        assert first_line.y0 == 17.0

    def test_empty_response(self):
        adapter = PPStructureAdapter(api_url="http://localhost:8006/ocr")
        result = adapter.parse_response({})

        assert isinstance(result, OCRPageResult)
        assert result.lines == []
        assert result.width == 0
        assert result.height == 0

    def test_blocks_empty_for_new_format(self):
        adapter = PPStructureAdapter(api_url="http://localhost:8006/ocr")
        page_data = SAMPLE_API_RESPONSE["results"][0]
        result = adapter.parse_response(page_data)

        assert result.blocks == []

    def test_polys_extracted(self):
        adapter = PPStructureAdapter(api_url="http://localhost:8006/ocr")
        page_data = SAMPLE_API_RESPONSE["results"][0]
        result = adapter.parse_response(page_data)

        first_line = result.lines[0]
        assert len(first_line.polys) == 4
        assert first_line.polys[0] == (20.0, 17.0)
        assert first_line.polys[1] == (352.0, 17.0)

    def test_raw_stored(self):
        adapter = PPStructureAdapter(api_url="http://localhost:8006/ocr")
        page_data = SAMPLE_API_RESPONSE["results"][0]
        result = adapter.parse_response(page_data)

        assert result.raw is not None
        assert "rec_texts" in result.raw

    def test_word_level_data(self):
        adapter = PPStructureAdapter(api_url="http://localhost:8006/ocr")
        page_data = SAMPLE_API_RESPONSE["results"][0]
        result = adapter.parse_response(page_data)

        first_line = result.lines[0]
        assert len(first_line.chars) == 3
        assert first_line.chars[0]["text"] == "软件"
        assert first_line.chars[0]["x0"] == 20.0
        assert first_line.chars[0]["y0"] == 17.0
        assert first_line.chars[0]["x1"] == 120.0
        assert first_line.chars[0]["y1"] == 76.0


class TestParseBlocks:
    """Layout block parsing from legacy and current PPStructure responses."""

    @staticmethod
    def _adapter():
        return PPStructureAdapter(api_url="http://localhost:8006/ocr")

    def test_legacy_parsing_res_list(self):
        page_data = {
            "rec_texts": ["标题"],
            "rec_scores": [0.99],
            "rec_boxes": [[0, 0, 200, 50]],
            "parsing_res_list": [
                {"block_label": "Paragraph_Title", "block_bbox": [0, 0, 200, 50]},
                {"layout_label": "text", "block_bbox": [0, 60, 400, 80]},
            ],
        }
        result = self._adapter().parse_response(page_data)

        assert len(result.blocks) == 2
        # Labels are lowercased to match BLOCK_LABEL_TO_OUTLINE keys.
        assert result.blocks[0].label == "paragraph_title"
        assert result.blocks[0].x1 == 200.0
        assert result.blocks[1].label == "text"

    def test_current_layout_det_res(self):
        page_data = {
            "rec_texts": ["表格内容"],
            "rec_scores": [0.9],
            "rec_boxes": [[1, 2, 300, 400]],
            "layout_det_res": [{"label": "Table", "bbox": [1, 2, 300, 400]}],
        }
        result = self._adapter().parse_response(page_data)

        assert len(result.blocks) == 1
        assert result.blocks[0].label == "table"

    def test_no_block_fields_stays_empty(self):
        page_data = {
            "rec_texts": ["正文"],
            "rec_scores": [0.9],
            "rec_boxes": [[0, 0, 100, 10]],
        }
        result = self._adapter().parse_response(page_data)

        assert result.blocks == []


class TestFactory:
    """Test OCR engine factory."""

    def test_creates_ppstructure_adapter(self):
        engine = create_ocr_engine("ppstructure", "http://localhost:8006/ocr")
        assert isinstance(engine, PPStructureAdapter)

    def test_unknown_engine_raises_value_error(self):
        with pytest.raises(ValueError, match="Unknown OCR engine"):
            create_ocr_engine("unknown_engine", "http://localhost:8006/ocr")


class TestPPStructureRecognizeHttp:
    """HTTP layer of PPStructureAdapter.recognize (mocked requests)."""

    def _adapter(self):
        return PPStructureAdapter(api_url="http://localhost:8006/ocr")

    def _fake_response(self, json_payload=None, json_exc=None):
        from unittest.mock import MagicMock

        response = MagicMock()
        response.raise_for_status.return_value = None
        if json_exc is not None:
            response.json.side_effect = json_exc
        else:
            response.json.return_value = json_payload
        return response

    @pytest.mark.parametrize(
        "suffix,expected_mime",
        [
            (".png", "image/png"),
            (".jpg", "image/jpeg"),
            (".jpeg", "image/jpeg"),
            (".tiff", "image/tiff"),
            (".tif", "image/tiff"),
            (".bmp", "image/bmp"),
        ],
    )
    def test_upload_mime_matches_file_suffix(self, monkeypatch, tmp_path, suffix, expected_mime):
        img = tmp_path / f"scan{suffix}"
        img.write_bytes(b"fake image bytes")
        captured = {}

        def fake_post(url, files=None, timeout=None):
            captured["files"] = files
            return self._fake_response(json_payload={"results": []})

        monkeypatch.setattr("docparse.parsers.ocr.ppstructure.requests.post", fake_post)

        result = self._adapter().recognize(str(img))

        assert result.lines == []
        _filename, _fileobj, mime = captured["files"]["file"]
        assert mime == expected_mime

    def test_non_json_response_wrapped_as_runtime_error(self, monkeypatch, tmp_path):
        img = tmp_path / "scan.png"
        img.write_bytes(b"fake image bytes")

        def fake_post(url, files=None, timeout=None):
            return self._fake_response(json_exc=ValueError("Expecting value: line 1 column 1"))

        monkeypatch.setattr("docparse.parsers.ocr.ppstructure.requests.post", fake_post)

        with pytest.raises(RuntimeError, match="non-JSON"):
            self._adapter().recognize(str(img))

    def test_http_error_wrapped_as_runtime_error(self, monkeypatch, tmp_path):
        import requests as _requests

        img = tmp_path / "scan.png"
        img.write_bytes(b"fake image bytes")

        def fake_post(url, files=None, timeout=None):
            response = self._fake_response()
            response.raise_for_status.side_effect = _requests.HTTPError("503 Service Unavailable")
            return response

        monkeypatch.setattr("docparse.parsers.ocr.ppstructure.requests.post", fake_post)

        with pytest.raises(RuntimeError, match="PPStructureV3 API call failed"):
            self._adapter().recognize(str(img))
