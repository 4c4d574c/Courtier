"""Tests for scanned parser with integrated OCR, font detection, and spacing."""


from docparse.parsers.ocr.base import OCRBlock, OCRLineResult, OCRPageResult
from docparse.parsers.scanned import ScannedParser
from docparse.parsers.scanned.font_detector import merge_font_info as _merge_font_info
from docparse.parsers.scanned.ocr_engine import (
    build_block_outline_map_from_blocks as _build_block_outline_map_from_blocks,
    get_outline_for_line as _get_outline_for_line,
    ocr_result_to_lines as _ocr_result_to_lines,
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
            {"line_no": 0, "text": "标题", "font_family": "", "font_weight": False, "font_style": False},
            {"line_no": 1, "text": "正文", "font_family": "", "font_weight": False, "font_style": False},
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
            {"line_no": 0, "text": "标题", "font_family": "宋体", "font_weight": False, "font_style": False},
        ]
        font_info = {
            0: {"font_family": "黑体", "font_weight": True, "font_style": False},
        }
        _merge_font_info(lines, font_info)
        assert lines[0]["font_family"] == "宋体"
        assert lines[0]["font_weight"] is True

    def test_skips_lines_not_in_font_info(self):
        lines = [
            {"line_no": 0, "text": "标题", "font_family": "", "font_weight": False, "font_style": False},
            {"line_no": 1, "text": "正文", "font_family": "", "font_weight": False, "font_style": False},
        ]
        font_info = {
            0: {"font_family": "黑体", "font_weight": True, "font_style": False},
        }
        _merge_font_info(lines, font_info)
        assert lines[0]["font_family"] == "黑体"
        assert lines[1]["font_family"] == ""

    def test_empty_font_info_does_nothing(self):
        lines = [
            {"line_no": 0, "text": "标题", "font_family": "", "font_weight": False, "font_style": False},
        ]
        _merge_font_info(lines, {})
        assert lines[0]["font_family"] == ""

    def test_merges_font_style(self):
        lines = [
            {"line_no": 0, "text": "引用", "font_family": "", "font_weight": False, "font_style": False},
        ]
        font_info = {
            0: {"font_family": "楷体", "font_weight": False, "font_style": True},
        }
        _merge_font_info(lines, font_info)
        assert lines[0]["font_style"] is True
