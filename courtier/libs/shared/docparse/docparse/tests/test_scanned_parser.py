"""Tests for scanned parser with integrated OCR, font detection, and spacing."""

import threading
import time
from types import SimpleNamespace

import docparse.parsers.scanned as scanned_mod
import pytest
from docmodels import PageContent
from docparse.parsers.base import ParserConfig
from docparse.parsers.ocr.base import OCRBlock, OCRLineResult, OCRPageResult
from docparse.parsers.scanned import ScannedParser
from docparse.parsers.scanned.font_detector import merge_font_info as _merge_font_info
from docparse.parsers.scanned.ocr_engine import (
    build_block_outline_map_from_blocks as _build_block_outline_map_from_blocks,
)
from docparse.parsers.scanned.ocr_engine import (
    estimate_font_size_from_word_boxes as _estimate_font_size_from_word_boxes,
)
from docparse.parsers.scanned.ocr_engine import get_outline_for_line as _get_outline_for_line
from docparse.parsers.scanned.ocr_engine import ocr_result_to_lines as _ocr_result_to_lines
from docparse.parsers.spacing import compute_font_size_from_ocr
from PIL import Image as PILImage


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch):
    """Keep OCR retry backoff out of test wall-clock time."""
    monkeypatch.setattr(
        "docparse.parsers._retry.time",
        SimpleNamespace(sleep=lambda *_a, **_k: None),
    )


SAMPLE_OCR_PAGE_RESULT = OCRPageResult(
    width=1472,
    height=943,
    lines=[
        OCRLineResult(
            text="软件开发模式：",
            line_no=0,
            x0=20.0,
            y0=17.0,
            x1=352.0,
            y1=76.0,
            polys=[(20, 17), (352, 17), (352, 76), (20, 76)],
            confidence=0.98,
        ),
        OCRLineResult(
            text="软件开发模式是指...",
            line_no=1,
            x0=22.0,
            y0=196.0,
            x1=1364.0,
            y1=235.0,
            polys=[(22, 196), (1364, 197), (1364, 235), (22, 234)],
            confidence=0.97,
        ),
    ],
    blocks=[
        OCRBlock(
            label="paragraph_title",
            x0=16.0,
            y0=19.0,
            x1=351.0,
            y1=71.0,
        ),
        OCRBlock(
            label="text",
            x0=15.0,
            y0=196.0,
            x1=1365.0,
            y1=281.0,
        ),
    ],
)


class TestScannedParserSupports:
    """Test ScannedParser.supports() method."""

    def test_supports_png(self):
        parser = ScannedParser()
        assert parser.supports("test.png") is True

    def test_supports_jpg(self):
        parser = ScannedParser()
        assert parser.supports("test.jpg") is True

    def test_supports_pdf(self):
        parser = ScannedParser()
        assert parser.supports("test.pdf") is True

    def test_does_not_support_docx(self):
        parser = ScannedParser()
        assert parser.supports("test.docx") is False

    def test_does_not_support_txt(self):
        parser = ScannedParser()
        assert parser.supports("test.txt") is False


class TestOcrResultToLines:
    """Test _ocr_result_to_lines function."""

    def test_converts_ocr_page_result_to_lines(self):
        lines = _ocr_result_to_lines(SAMPLE_OCR_PAGE_RESULT)
        assert len(lines) == 2
        assert lines[0]["text"] == "软件开发模式："
        assert lines[0]["line_no"] == 0
        assert lines[0]["x0"] == 20.0
        assert lines[0]["confidence"] == 0.98
        assert lines[0]["outline_level"] == "heading2"
        assert lines[0]["alignment"] == "left"
        assert lines[0]["font_size"] > 0

    def test_empty_result_returns_empty_list(self):
        empty_result = OCRPageResult(lines=[], width=0, height=0)
        lines = _ocr_result_to_lines(empty_result)
        assert lines == []

    def test_skips_empty_text_lines(self):
        result = OCRPageResult(
            width=100,
            height=100,
            lines=[
                OCRLineResult(text="", line_no=0, x0=0, y0=0, x1=50, y1=20),
                OCRLineResult(text="有效文本", line_no=1, x0=0, y0=30, x1=50, y1=50),
            ],
        )
        lines = _ocr_result_to_lines(result)
        assert len(lines) == 1
        assert lines[0]["text"] == "有效文本"

    def test_line_with_no_polys(self):
        result = OCRPageResult(
            width=1000,
            height=1000,
            lines=[
                OCRLineResult(
                    text="无多边形",
                    line_no=0,
                    x0=10,
                    y0=10,
                    x1=200,
                    y1=50,
                    polys=[],
                    confidence=0.95,
                ),
            ],
        )
        lines = _ocr_result_to_lines(result)
        assert len(lines) == 1
        assert lines[0]["font_size"] > 0

    def test_text_with_no_blocks_gets_others_outline(self):
        result = OCRPageResult(
            width=1000,
            height=1000,
            lines=[
                OCRLineResult(
                    text="孤立文本",
                    line_no=0,
                    x0=10,
                    y0=10,
                    x1=200,
                    y1=50,
                    confidence=0.95,
                ),
            ],
            blocks=[],
        )
        lines = _ocr_result_to_lines(result)
        assert len(lines) == 1
        assert lines[0]["outline_level"] == "others"

    def test_img_dimensions_override_page_result(self):
        """传入 img_width/img_height 时，对齐与字号以传入值为准。"""
        result = OCRPageResult(
            width=350,
            height=200,
            lines=[
                OCRLineResult(
                    text="正文内容行行行",
                    line_no=0,
                    x0=50.0,
                    y0=100.0,
                    x1=350.0,
                    y1=120.0,
                    confidence=0.99,
                ),
            ],
        )
        # page_result 尺寸：width_ratio = 300/350 ≈ 0.86 → justify
        default_lines = _ocr_result_to_lines(result)
        assert default_lines[0]["alignment"] == "justify"

        # 传入真实图像尺寸：width_ratio = 300/1000 = 0.30 → 非 justify，
        # left_ratio = 0.05 → left；字号按 img_height=1000 换算，明显不同。
        lines = _ocr_result_to_lines(result, img_width=1000, img_height=1000)
        assert lines[0]["alignment"] == "left"
        expected = compute_font_size_from_ocr(20.0, 1000)
        assert lines[0]["font_size"] == round(expected, 1)
        assert lines[0]["font_size"] != default_lines[0]["font_size"]

    def test_polys_used_for_font_size(self):
        """歪斜行的字号按 polys 四边形边高计算，而非 bbox 虚高。"""
        polys = [(10, 10), (200, 15), (200, 50), (10, 45)]
        result = OCRPageResult(
            width=1000,
            height=1000,
            lines=[
                OCRLineResult(
                    text="歪斜文本行",
                    line_no=0,
                    x0=10.0,
                    y0=10.0,
                    x1=200.0,
                    y1=70.0,
                    polys=polys,
                    confidence=0.95,
                ),
            ],
        )
        lines = _ocr_result_to_lines(result)
        # polys 左右边高均 35px，bbox 高 60px —— 两者算出的字号必须不同，
        # 且结果与 polys 计算一致。
        expected = compute_font_size_from_ocr(60.0, 1000, polys=polys)
        bbox_only = compute_font_size_from_ocr(60.0, 1000)
        assert expected != bbox_only
        assert lines[0]["font_size"] == round(expected, 1)


class TestEstimateFontSizeFromWordBoxes:
    """词框字宽法：长 CJK 行（标题）用逐字行进宽度测字号。"""

    @staticmethod
    def _word_boxes(words: list[str], char_px: float) -> list[dict]:
        boxes = []
        x = 0.0
        for word in words:
            width = char_px * (0.5 if all(ord(c) < 128 for c in word) else len(word))
            boxes.append({"text": word, "x0": x, "y0": 0.0, "x1": x + width, "y1": 40.0})
            x += width
        return boxes

    def test_exact_advance_calibrates(self):
        """每个 CJK 词框宽 = 2×字号像素时精确命中。"""
        # 22pt at 1240px 页宽：22 / (595.28/1240) ≈ 45.8px/字
        words = ["关于", "提供", "有关", "工作", "情况", "的函"]
        chars = self._word_boxes(words, 45.82)
        size = _estimate_font_size_from_word_boxes(chars, 1240.0)
        assert size == 22.0

    def test_short_line_ineligible(self):
        """短行（<8 有效字）回退 None。"""
        chars = self._word_boxes(["密级", "长期"], 33.0)
        assert _estimate_font_size_from_word_boxes(chars, 1240.0) is None

    def test_digit_heavy_line_ineligible(self):
        """数字为主的行（日期/发文字号）CJK 占比不足，回退 None。"""
        chars = self._word_boxes(["2026", "年", "4", "月", "19", "日"], 20.0)
        assert _estimate_font_size_from_word_boxes(chars, 1240.0) is None

    def test_empty_inputs(self):
        assert _estimate_font_size_from_word_boxes([], 1240.0) is None
        chars = self._word_boxes(["关于开展专项整治行动的请示"], 45.0)
        assert _estimate_font_size_from_word_boxes(chars, 0.0) is None

    def test_ocr_result_to_lines_prefers_word_boxes(self):
        """端到端：带词框的标题行字号取词框法，而非框高法。"""
        words = ["关于", "提供", "有关", "工作", "情况", "的函"]
        x = 100.0
        chars = []
        for word in words:
            width = 45.82 * len(word)
            chars.append({"text": word, "x0": x, "y0": 0.0, "x1": x + width, "y1": 40.0})
            x += width
        result = OCRPageResult(
            width=1240,
            height=1754,
            lines=[
                OCRLineResult(
                    text="关于提供有关工作情况的函",
                    line_no=0,
                    x0=100.0,
                    y0=100.0,
                    x1=x,
                    y1=130.0,
                    confidence=0.99,
                    chars=chars,
                ),
            ],
        )
        lines = _ocr_result_to_lines(result)
        assert len(lines) == 1
        assert lines[0]["font_size"] == 22.0


class TestBuildBlockOutlineMapFromBlocks:
    """Test _build_block_outline_map_from_blocks function."""

    def test_maps_ocr_blocks(self):
        blocks = SAMPLE_OCR_PAGE_RESULT.blocks
        result = _build_block_outline_map_from_blocks(blocks)
        assert len(result) == 2
        assert result[0] == (16.0, 19.0, 351.0, 71.0, "heading2")
        assert result[1] == (15.0, 196.0, 1365.0, 281.0, "others")

    def test_empty_blocks_returns_empty_list(self):
        result = _build_block_outline_map_from_blocks([])
        assert result == []

    def test_unknown_label_maps_to_others(self):
        blocks = [
            OCRBlock(label="unknown_label", x0=0, y0=0, x1=100, y1=100),
        ]
        result = _build_block_outline_map_from_blocks(blocks)
        assert len(result) == 1
        assert result[0][4] == "others"


class TestGetOutlineForLine:
    """Test _get_outline_for_line function."""

    def test_line_in_block(self):
        block_map = [(10, 10, 400, 80, "heading2")]
        result = _get_outline_for_line(200, 40, block_map)
        assert result == "heading2"

    def test_line_not_in_any_block(self):
        block_map = [(10, 10, 400, 80, "heading2")]
        result = _get_outline_for_line(500, 500, block_map)
        assert result == "others"

    def test_empty_block_map_returns_others(self):
        result = _get_outline_for_line(200, 40, [])
        assert result == "others"


class TestMergeFontInfo:
    """Test _merge_font_info function."""

    def test_merges_font_family_into_empty_lines(self):
        lines = [
            {
                "line_no": 0,
                "text": "标题",
                "font_family": "",
                "font_weight": False,
                "font_style": False,
            },
            {
                "line_no": 1,
                "text": "正文",
                "font_family": "",
                "font_weight": False,
                "font_style": False,
            },
        ]
        font_info = {
            0: {"font_family": "黑体", "font_weight": True, "font_style": False},
            1: {"font_family": "仿宋", "font_weight": False, "font_style": False},
        }
        _merge_font_info(lines, font_info)
        assert lines[0]["font_family"] == "黑体"
        assert lines[0]["font_weight"] is True
        assert lines[1]["font_family"] == "仿宋"
        assert lines[1]["font_weight"] is False

    def test_preserves_existing_font_family(self):
        lines = [
            {
                "line_no": 0,
                "text": "标题",
                "font_family": "宋体",
                "font_weight": False,
                "font_style": False,
            },
        ]
        font_info = {
            0: {"font_family": "黑体", "font_weight": True, "font_style": False},
        }
        _merge_font_info(lines, font_info)
        assert lines[0]["font_family"] == "宋体"
        assert lines[0]["font_weight"] is True

    def test_skips_lines_not_in_font_info(self):
        lines = [
            {
                "line_no": 0,
                "text": "标题",
                "font_family": "",
                "font_weight": False,
                "font_style": False,
            },
            {
                "line_no": 1,
                "text": "正文",
                "font_family": "",
                "font_weight": False,
                "font_style": False,
            },
        ]
        font_info = {
            0: {"font_family": "黑体", "font_weight": True, "font_style": False},
        }
        _merge_font_info(lines, font_info)
        assert lines[0]["font_family"] == "黑体"
        assert lines[1]["font_family"] == ""

    def test_empty_font_info_does_nothing(self):
        lines = [
            {
                "line_no": 0,
                "text": "标题",
                "font_family": "",
                "font_weight": False,
                "font_style": False,
            },
        ]
        _merge_font_info(lines, {})
        assert lines[0]["font_family"] == ""

    def test_merges_font_style(self):
        lines = [
            {
                "line_no": 0,
                "text": "引用",
                "font_family": "",
                "font_weight": False,
                "font_style": False,
            },
        ]
        font_info = {
            0: {"font_family": "楷体", "font_weight": False, "font_style": True},
        }
        _merge_font_info(lines, font_info)
        assert lines[0]["font_style"] is True


# ---------------------------------------------------------------------------
# parallel_ocr failure propagation
# ---------------------------------------------------------------------------


class _StubOcrEngine:
    """OCR engine stub: maps image path to a result or an exception."""

    def __init__(self, behavior: dict):
        self._behavior = behavior
        self.calls: list[str] = []

    def recognize(self, image_path: str) -> OCRPageResult:
        self.calls.append(image_path)
        outcome = self._behavior[image_path]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _FlakyOnceOcrEngine:
    """OCR engine stub: fails the first N calls per path, then succeeds."""

    def __init__(self, fail_times: int = 1):
        self._fail_times = fail_times
        self.calls: list[str] = []

    def recognize(self, image_path: str) -> OCRPageResult:
        self.calls.append(image_path)
        if self.calls.count(image_path) <= self._fail_times:
            raise RuntimeError("transient OCR boom")
        return _ok_result()


def _ok_result(
    width: int = 400,
    height: int = 600,
    blocks: list | None = None,
) -> OCRPageResult:
    return OCRPageResult(
        width=width,
        height=height,
        lines=[
            OCRLineResult(
                text="正文内容行",
                line_no=0,
                x0=50.0,
                y0=100.0,
                x1=350.0,
                y1=120.0,
                confidence=0.99,
            )
        ],
        blocks=blocks or [],
    )


class TestParallelOcrFailures:
    """OCR failure propagation in parallel_ocr."""

    def test_empty_image_paths_returns_empty(self):
        from docparse.parsers.scanned.ocr_engine import parallel_ocr

        assert parallel_ocr(_StubOcrEngine({}), [], 4) == []

    def test_single_page_failure_raises(self):
        from docparse.parsers.scanned.ocr_engine import parallel_ocr

        engine = _StubOcrEngine({"p0.png": RuntimeError("OCR down")})
        warnings: list[str] = []
        with pytest.raises(RuntimeError, match="OCR 识别均失败"):
            parallel_ocr(engine, ["p0.png"], 4, warnings=warnings)
        assert len(warnings) == 1
        assert "第 1 页 OCR 识别失败" in warnings[0]

    def test_all_pages_fail_raises(self):
        from docparse.parsers.scanned.ocr_engine import parallel_ocr

        paths = ["p0.png", "p1.png"]
        engine = _StubOcrEngine({p: RuntimeError("OCR down") for p in paths})
        warnings: list[str] = []
        with pytest.raises(RuntimeError, match="所有 2 页 OCR 识别均失败"):
            parallel_ocr(engine, paths, 4, warnings=warnings)
        assert len(warnings) == 2
        assert any("第 1 页" in w for w in warnings)
        assert any("第 2 页" in w for w in warnings)

    def test_partial_failure_keeps_results_and_warns(self):
        from docparse.parsers.scanned.ocr_engine import parallel_ocr

        paths = ["p0.png", "p1.png", "p2.png"]
        engine = _StubOcrEngine(
            {
                "p0.png": _ok_result(),
                "p1.png": RuntimeError("OCR boom"),
                "p2.png": _ok_result(),
            }
        )
        warnings: list[str] = []
        failed_pages: list[int] = []
        results = parallel_ocr(engine, paths, 4, warnings=warnings, failed_pages=failed_pages)

        # Results stay in page order; failed page degrades to empty.
        assert len(results) == 3
        assert len(results[0].lines) == 1
        assert results[1].lines == []
        assert len(results[2].lines) == 1
        assert failed_pages == [1]
        assert len(warnings) == 1
        assert "第 2 页 OCR 识别失败" in warnings[0]
        assert "OCR boom" in warnings[0]

    def test_transient_failure_retried_and_recovers(self):
        """One retry per page: a first-attempt transient failure is absorbed."""
        from docparse.parsers.scanned.ocr_engine import parallel_ocr

        engine = _FlakyOnceOcrEngine(fail_times=1)
        warnings: list[str] = []
        results = parallel_ocr(engine, ["p0.png"], 4, warnings=warnings)

        assert len(results) == 1
        assert len(results[0].lines) == 1
        # First attempt raises, the retry succeeds — no warning at all.
        assert engine.calls == ["p0.png", "p0.png"]
        assert warnings == []

    def test_retry_exhausted_degrades_page(self):
        """A page failing both attempts degrades with a warning."""
        from docparse.parsers.scanned.ocr_engine import parallel_ocr

        paths = ["p0.png", "p1.png"]
        engine = _StubOcrEngine(
            {
                "p0.png": RuntimeError("OCR boom"),
                "p1.png": _ok_result(),
            }
        )
        warnings: list[str] = []
        failed_pages: list[int] = []
        results = parallel_ocr(engine, paths, 4, warnings=warnings, failed_pages=failed_pages)

        assert failed_pages == [0]
        assert results[0].lines == []
        assert len(results[1].lines) == 1
        assert len(warnings) == 1
        assert "OCR boom" in warnings[0]
        # The failing page was attempted twice (1 retry); the healthy page once.
        assert engine.calls.count("p0.png") == 2
        assert engine.calls.count("p1.png") == 1


# ---------------------------------------------------------------------------
# ScannedParser pipeline tests (mocked OCR engine / LLM client)
# ---------------------------------------------------------------------------


def _write_png(path, size=(400, 600)):
    PILImage.new("RGB", size, 255).save(str(path))


class _FakeLLMClient:
    """LLM client stub returning a minimal valid structure."""

    def __init__(self, config):
        pass

    def recognize_structure(self, lines, image_path):
        return {
            "header": None,
            "body": {
                "main_text": [
                    {
                        "text": "".join(line["text"] for line in lines),
                        "line_indices": list(range(len(lines))),
                        "outline_level": "body_text",
                    }
                ]
            },
            "footer": None,
        }

    def recognize_fonts_from_crops(self, page_image_path, lines):
        return {}


def _patch_pipeline(monkeypatch, image_files, ocr_engine, llm_cls=_FakeLLMClient):
    monkeypatch.setattr(scanned_mod, "prepare_images", lambda *_a, **_k: image_files)
    monkeypatch.setattr(scanned_mod, "resize_images_for_ocr", lambda paths, _max: paths)
    monkeypatch.setattr(scanned_mod, "create_ocr_engine", lambda *_a: ocr_engine)
    monkeypatch.setattr(scanned_mod, "LLMClient", llm_cls)


def _make_config(**overrides) -> ParserConfig:
    base = {
        "llm_api_key": "test-key",
        "llm_base_url": "http://localhost:1/v1",
        "llm_model": "test-model",
        "ocr_api_url": "http://localhost:1/ocr",
    }
    base.update(overrides)
    return ParserConfig(**base)


class TestScannedParserPipeline:
    """End-to-end parse() behavior with mocked OCR/LLM backends."""

    def test_fail_fast_without_llm_api_key(self, monkeypatch, tmp_path):
        """No LLM key -> ValueError before any image prep or OCR call."""
        src = tmp_path / "doc.png"
        _write_png(src)

        calls = {"prepare": 0, "ocr_factory": 0}

        def _prepare(_p):
            calls["prepare"] += 1
            return [str(src)]

        def _factory(*_a):
            calls["ocr_factory"] += 1
            raise AssertionError("OCR engine must not be created")

        monkeypatch.setattr(scanned_mod, "prepare_images", _prepare)
        monkeypatch.setattr(scanned_mod, "create_ocr_engine", _factory)

        with pytest.raises(ValueError, match="LLM_API_KEY"):
            ScannedParser().parse(str(src), _make_config(llm_api_key=""))

        assert calls == {"prepare": 0, "ocr_factory": 0}

    def test_all_pages_ocr_failure_raises(self, monkeypatch, tmp_path):
        pages = []
        for i in range(2):
            p = tmp_path / f"page_{i}.png"
            _write_png(p)
            pages.append(str(p))

        engine = _StubOcrEngine({p: RuntimeError("service down") for p in pages})
        _patch_pipeline(monkeypatch, pages, engine)

        with pytest.raises(RuntimeError, match="OCR 识别均失败"):
            ScannedParser().parse(pages[0], _make_config())

    def test_partial_ocr_failure_degrades_with_warnings(self, monkeypatch, tmp_path):
        pages = []
        for i in range(3):
            p = tmp_path / f"page_{i}.png"
            _write_png(p)
            pages.append(str(p))

        engine = _StubOcrEngine(
            {
                pages[0]: _ok_result(),
                pages[1]: RuntimeError("OCR boom"),
                pages[2]: _ok_result(),
            }
        )
        _patch_pipeline(monkeypatch, pages, engine)

        doc = ScannedParser().parse(pages[0], _make_config())

        assert doc.total_page_num == 3
        assert [p.page_no for p in doc.pages] == [0, 1, 2]
        # Failed page is empty but present; other pages keep their content.
        assert doc.pages[1].page_content.body.main_text == []
        assert len(doc.pages[0].page_content.body.main_text) == 1
        assert len(doc.pages[2].page_content.body.main_text) == 1
        assert any("第 2 页 OCR 识别失败" in w for w in doc.warnings)
        # Page model no longer carries raw bytes / per-page save_path
        from docmodels import Page

        assert "raw" not in Page.model_fields
        assert "save_path" not in Page.model_fields

    def test_table_block_adds_warning(self, monkeypatch, tmp_path):
        """A page whose OCR blocks include a table region gets a warning."""
        src = tmp_path / "page_0.png"
        _write_png(src)
        pages = [str(src)]

        table_block = OCRBlock(label="table", x0=0, y0=0, x1=50, y1=50)
        engine = _StubOcrEngine({pages[0]: _ok_result(blocks=[table_block])})
        _patch_pipeline(monkeypatch, pages, engine)

        doc = ScannedParser().parse(pages[0], _make_config())

        table_warnings = [w for w in doc.warnings if "表格区域" in w]
        assert len(table_warnings) == 1
        assert "第 1 页检测到表格区域" in table_warnings[0]

    def test_parse_pages_subset_maps_original_page_numbers(self, monkeypatch, tmp_path):
        """parse_pages 只处理子集页，Page.page_no 与告警页码均为原始页码。"""
        pages = []
        for i in range(2):
            p = tmp_path / f"page_{i}.png"
            _write_png(p)
            pages.append(str(p))

        # 两张子集图（模拟原始第 3、5 页）：第一张正常，第二张 OCR 失败
        engine = _StubOcrEngine({pages[0]: _ok_result(), pages[1]: RuntimeError("OCR boom")})
        _patch_pipeline(monkeypatch, pages, engine)

        result_pages, warnings = ScannedParser().parse_pages(str(pages[0]), [2, 4], _make_config())

        assert [p.page_no for p in result_pages] == [2, 4]
        assert len(result_pages[0].page_content.body.main_text) == 1
        # 失败页降级为空页；OCR 失败告警引用原始页码（第 5 页，非子集序号）
        assert result_pages[1].page_content.body.main_text == []
        assert any("第 5 页 OCR 识别失败" in w for w in warnings)
        assert not any("第 2 页 OCR 识别失败" in w for w in warnings)

    def test_llm_structure_failure_falls_back_to_rules(self, monkeypatch, tmp_path):
        """A page whose LLM call fails is classified by the rule engine."""
        pages = []
        for i in range(2):
            p = tmp_path / f"page_{i}.png"
            _write_png(p)
            pages.append(str(p))

        engine = _StubOcrEngine({p: _ok_result() for p in pages})

        class _FlakyLLMClient(_FakeLLMClient):
            def recognize_structure(self, lines, image_path):
                if "page_1" in image_path:
                    raise RuntimeError("LLM boom")
                return super().recognize_structure(lines, image_path)

        _patch_pipeline(monkeypatch, pages, engine, llm_cls=_FlakyLLMClient)

        doc = ScannedParser().parse(pages[0], _make_config())

        assert doc.total_page_num == 2
        assert any("第 2 页 LLM 结构识别失败" in w and "规则引擎" in w for w in doc.warnings)
        # Fallback keeps the page's text content instead of dropping it.
        from docparse.parsers.scanned.structure import collect_all_paragraphs

        fallback_text = "".join(
            elem.font.text
            for para in collect_all_paragraphs(doc.pages[1].page_content)
            for elem in para.elements
        )
        assert "正文内容行" in fallback_text

    def test_llm_calls_respect_max_concurrency(self, monkeypatch, tmp_path):
        """Structure recognition runs concurrently, bounded by config."""
        pages = []
        for i in range(4):
            p = tmp_path / f"page_{i}.png"
            _write_png(p)
            pages.append(str(p))

        engine = _StubOcrEngine({p: _ok_result() for p in pages})
        _patch_pipeline(monkeypatch, pages, engine)

        lock = threading.Lock()
        stats = {"current": 0, "max_seen": 0}

        def _tracked_recognize(lines, margin, llm_client, image_path, config=None, img_width=0.0):
            with lock:
                stats["current"] += 1
                stats["max_seen"] = max(stats["max_seen"], stats["current"])
            try:
                time.sleep(0.05)
            finally:
                with lock:
                    stats["current"] -= 1
            return PageContent(margin=margin)

        monkeypatch.setattr(scanned_mod, "recognize_page_structure", _tracked_recognize)

        doc = ScannedParser().parse(pages[0], _make_config(max_llm_concurrent=2))

        assert stats["max_seen"] == 2
        assert [p.page_no for p in doc.pages] == [0, 1, 2, 3]
        assert doc.total_page_num == 4

    def test_font_recognition_receives_all_lines(self, monkeypatch, tmp_path):
        """裁剪字体识别接收每个有行页面的全部行（不再按 font_family 预过滤）。"""
        pages = []
        for i in range(2):
            p = tmp_path / f"page_{i}.png"
            _write_png(p)
            pages.append(str(p))

        engine = _StubOcrEngine({p: _ok_result() for p in pages})

        received: dict[str, list] = {}

        class _TrackingLLMClient(_FakeLLMClient):
            def recognize_fonts_from_crops(self, page_image_path, lines):
                received[page_image_path] = list(lines)
                return {}

        _patch_pipeline(monkeypatch, pages, engine, llm_cls=_TrackingLLMClient)

        ScannedParser().parse(pages[0], _make_config())

        # 每个有行的页面都被调用，且收到该页的全部 OCR 行
        assert set(received) == set(pages)
        for path in pages:
            assert len(received[path]) == 1
            assert received[path][0]["text"] == "正文内容行"
