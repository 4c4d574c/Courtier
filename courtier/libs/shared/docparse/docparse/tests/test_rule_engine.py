"""Unit tests for rule_patterns, page_regions, and rules modules."""

from __future__ import annotations

import pytest
from docparse.parsers.page_regions import split_docx_regions, split_pdf_regions
from docparse.parsers.rule_patterns import (
    ATTACHMENT_NOTE_PATTERN,
    CARBON_COPY_PATTERN,
    CLASSIFICATION_PATTERN,
    CONFIDENCE_SIGNAL_AGREEMENT,
    COPY_NUMBER_PATTERN,
    DEFAULT_CONFIDENCE_THRESHOLD,
    DISTRIBUTION_DATE_PATTERN,
    HEADING1_PATTERN,
    HEADING1_PATTERN_OCR,
    HEADING2_PATTERN,
    HEADING3_PATTERN,
    HEADING4_PATTERN,
    ISSUE_DATE_PATTERN,
    ISSUE_DATE_PATTERN_OCR,
    ISSUING_NUMBER_PATTERN,
    ISSUING_NUMBER_PATTERN_OCR,
    PAGE_NUMBER_PATTERN,
    SIGNATORY_PATTERN,
    URGENCY_PATTERN,
)
from docparse.parsers.rules import StructureRuleEngine

# ---------------------------------------------------------------------------
# Regex pattern tests
# ---------------------------------------------------------------------------


class TestHeadingPatterns:
    @pytest.mark.parametrize(
        "text",
        [
            "一、工作目标",
            "二、基本原则",
            "三、主要任务",
            "十、附则",
            "十二、其他规定",
        ],
    )
    def test_heading1_matches(self, text: str) -> None:
        assert HEADING1_PATTERN.match(text), f"Should match: {text}"

    @pytest.mark.parametrize(
        "text",
        [
            "工作目标",  # no prefix
            "（一）基本原则",  # heading2 format
            "1. 主要任务",  # heading3 format
        ],
    )
    def test_heading1_non_matches(self, text: str) -> None:
        assert not HEADING1_PATTERN.match(text), f"Should not match: {text}"

    @pytest.mark.parametrize(
        "text",
        [
            "（一）基本原则",
            "（二）主要任务",
            "(三)具体措施",
            "（十）综合评估",
        ],
    )
    def test_heading2_matches(self, text: str) -> None:
        assert HEADING2_PATTERN.match(text), f"Should match: {text}"

    @pytest.mark.parametrize(
        "text",
        [
            "一、工作目标",
            "1. 主要任务",
            "（1）具体要求",
        ],
    )
    def test_heading2_non_matches(self, text: str) -> None:
        assert not HEADING2_PATTERN.match(text), f"Should not match: {text}"

    @pytest.mark.parametrize(
        "text",
        [
            "1.主要任务",
            "1. 主要任务",
            "2.具体措施",
            "10. 长期规划",
            "3．工作安排",
        ],
    )
    def test_heading3_matches(self, text: str) -> None:
        assert HEADING3_PATTERN.match(text), f"Should match: {text}"

    @pytest.mark.parametrize(
        "text",
        [
            "（一）基本原则",
            "一、工作目标",
            "（1）具体要求",
        ],
    )
    def test_heading3_non_matches(self, text: str) -> None:
        assert not HEADING3_PATTERN.match(text), f"Should not match: {text}"

    @pytest.mark.parametrize(
        "text",
        [
            "（1）具体要求",
            "（2）工作措施",
            "(3)注意事项",
            "（10）补充规定",
        ],
    )
    def test_heading4_matches(self, text: str) -> None:
        assert HEADING4_PATTERN.match(text), f"Should match: {text}"


class TestHeaderFieldPatterns:
    @pytest.mark.parametrize("text", ["000001", "123", "123456", "001234"])
    def test_copy_number_matches(self, text: str) -> None:
        assert COPY_NUMBER_PATTERN.match(text), f"Should match: {text}"

    @pytest.mark.parametrize("text", ["12", "1234567", "abc123", ""])
    def test_copy_number_non_matches(self, text: str) -> None:
        assert not COPY_NUMBER_PATTERN.match(text), f"Should not match: {text}"

    @pytest.mark.parametrize("text", ["绝密", "机密★10年", "秘密★6个月", "绝密★"])
    def test_classification_matches(self, text: str) -> None:
        assert CLASSIFICATION_PATTERN.match(text), f"Should match: {text}"

    @pytest.mark.parametrize("text", ["特急", "加急", "急件"])
    def test_urgency_matches(self, text: str) -> None:
        assert URGENCY_PATTERN.match(text), f"Should match: {text}"

    @pytest.mark.parametrize(
        "text",
        [
            "国办发〔2024〕1号",
            "X委〔2023〕15号",
            "粤府办(2024)100号",
            "京政发[2024]1号",
        ],
    )
    def test_issuing_number_matches(self, text: str) -> None:
        assert ISSUING_NUMBER_PATTERN.match(text), f"Should match: {text}"

    @pytest.mark.parametrize(
        "text",
        [
            "签发人：张三",
            "签发人:李四",
            "签发人：王五、赵六",
        ],
    )
    def test_signatory_matches(self, text: str) -> None:
        assert SIGNATORY_PATTERN.match(text), f"Should match: {text}"


class TestBodyFieldPatterns:
    @pytest.mark.parametrize(
        "text",
        [
            "附件：关于xxx的通知",
            "附件：1. xxx\n2. yyy",
        ],
    )
    def test_attachment_note_matches(self, text: str) -> None:
        assert ATTACHMENT_NOTE_PATTERN.match(text), f"Should match: {text}"

    @pytest.mark.parametrize(
        "text",
        [
            "2024年1月15日",
            "2024年12月1日",
            "2023年5月31日",
        ],
    )
    def test_issue_date_matches(self, text: str) -> None:
        assert ISSUE_DATE_PATTERN.match(text), f"Should match: {text}"

    @pytest.mark.parametrize(
        "text",
        [
            "2024-01-15",
            "24年1月15日",
            "2024年1月",
        ],
    )
    def test_issue_date_non_matches(self, text: str) -> None:
        assert not ISSUE_DATE_PATTERN.match(text), f"Should not match: {text}"


class TestFooterFieldPatterns:
    @pytest.mark.parametrize(
        "text",
        [
            "抄送：省委办公厅，省政府办公厅。",
            "抄送:各部门。",
        ],
    )
    def test_carbon_copy_matches(self, text: str) -> None:
        assert CARBON_COPY_PATTERN.match(text), f"Should match: {text}"

    @pytest.mark.parametrize("text", ["- 1 -", "— 5 —", "3", "第3页", "-3-"])
    def test_page_number_matches(self, text: str) -> None:
        assert PAGE_NUMBER_PATTERN.match(text), f"Should match: {text}"

    @pytest.mark.parametrize(
        "text",
        [
            "2024年1月15日",
            "2024年12月1日",
        ],
    )
    def test_distribution_date_matches(self, text: str) -> None:
        assert DISTRIBUTION_DATE_PATTERN.search(text), f"Should match: {text}"


# ---------------------------------------------------------------------------
# Page region tests
# ---------------------------------------------------------------------------


def _make_line(text: str, line_no: int = 0, **kwargs: object) -> dict[str, object]:
    """Create a minimal line dict for testing."""
    base = {
        "text": text,
        "line_no": line_no,
        "font_family": "",
        "font_size": 0.0,
        "font_weight": False,
        "font_style": False,
        "alignment": "left",
        "style_name": "",
        "x0": 0.0,
        "y0": 0.0,
        "x1": 0.0,
        "y1": 0.0,
    }
    base.update(kwargs)
    return base


class TestDocxRegionSplit:
    def test_basic_split(self) -> None:
        lines = [
            _make_line("XX市人民政府办公室", 0, font_size=26.0, alignment="center"),
            _make_line("X办发〔2024〕1号", 1),
            _make_line("关于xxx的通知", 2, font_size=22.0, alignment="center"),
            _make_line("各部门：", 3),
            _make_line("一、工作目标", 4, font_family="黑体", font_weight=True),
            _make_line("正文内容...", 5, font_family="仿宋"),
            _make_line("抄送：xxx。", 6),
            _make_line("XX办公室        2024年1月15日印发", 7),
        ]
        header, body, footer = split_docx_regions(lines)
        assert len(header) == 2  # issuing logo + issuing number
        assert len(footer) == 2  # carbon copy + issuing office
        assert len(body) == 4  # title + addressee + heading + body text

    def test_no_header_markers(self) -> None:
        lines = [
            _make_line("标题内容", 0, font_size=22.0, alignment="center"),
            _make_line("正文内容", 1),
        ]
        header, body, footer = split_docx_regions(lines)
        assert len(header) == 1  # centered large text → issuing_logo candidate
        assert len(body) == 1

    def test_no_footer(self) -> None:
        lines = [
            _make_line("XX市人民政府办公室", 0, font_size=26.0, alignment="center"),
            _make_line("X办发〔2024〕1号", 1),
            _make_line("正文内容", 2),
        ]
        header, body, footer = split_docx_regions(lines)
        assert len(header) == 2
        assert len(body) == 1
        assert len(footer) == 0

    def test_empty_lines(self) -> None:
        header, body, footer = split_docx_regions([])
        assert header == []
        assert body == []
        assert footer == []


class TestPdfRegionSplit:
    def test_basic_split_by_y(self) -> None:
        lines = [
            _make_line("XX市人民政府", 0, y0=20, y1=50),
            _make_line("X办发〔2024〕1号", 1, y0=55, y1=70),
            # large gap (~130pt, header/body boundary)
            _make_line("标题", 2, y0=200, y1=225),
            _make_line("正文", 3, y0=230, y1=250),
            _make_line("更多正文", 4, y0=255, y1=275),
            # large gap near bottom (body/footer boundary)
            _make_line("抄送：xxx", 5, y0=720, y1=735),
            _make_line("- 1 -", 6, y0=750, y1=765),
        ]
        header, body, footer = split_pdf_regions(lines, page_height=841.89)
        # With large Y gaps, should split into regions
        assert (
            len(header) >= 1 or len(body) >= 3
        ), f"header={len(header)}, body={len(body)}, footer={len(footer)}"

    def test_fallback_to_content_when_no_position(self) -> None:
        lines = [
            _make_line("X办发〔2024〕1号", 0),
            _make_line("正文", 1),
        ]
        header, body, footer = split_pdf_regions(lines)
        # Should fallback to split_docx_regions
        assert len(header) >= 1


# ---------------------------------------------------------------------------
# Rule engine classification tests
# ---------------------------------------------------------------------------


class TestRuleEngineOutlineLevel:
    def setup_method(self) -> None:
        self.engine = StructureRuleEngine()

    def test_heading1_with_heiti_font(self) -> None:
        line = _make_line(
            "一、工作目标",
            font_family="黑体",
            font_weight=True,
            style_name="Normal",
        )
        outline, conf = self.engine._classify_outline_level(line)
        assert outline == "heading1"
        assert conf >= 0.85

    def test_heading1_without_font_info(self) -> None:
        line = _make_line("二、基本原则", style_name="Normal")
        outline, conf = self.engine._classify_outline_level(line)
        assert outline == "heading1"
        # Without font info, confidence is base+regex only (0.60)
        assert conf >= 0.55

    def test_heading2_with_kaiti_font(self) -> None:
        line = _make_line(
            "（一）基本原则",
            font_family="楷体",
            font_weight=True,
            style_name="Normal",
        )
        outline, conf = self.engine._classify_outline_level(line)
        assert outline == "heading2"
        assert conf >= 0.85

    def test_heading3_with_bold(self) -> None:
        line = _make_line(
            "1.主要任务",
            font_family="仿宋",
            font_weight=True,
            style_name="Normal",
        )
        outline, conf = self.engine._classify_outline_level(line)
        assert outline == "heading3"
        assert conf >= 0.80

    def test_heading4_pattern(self) -> None:
        line = _make_line("（1）具体要求", style_name="Normal")
        outline, conf = self.engine._classify_outline_level(line)
        assert outline == "heading4"

    def test_body_text_no_heading(self) -> None:
        line = _make_line("这是正文内容", font_family="仿宋")
        outline, conf = self.engine._classify_outline_level(line)
        assert outline == ""

    def test_heading_with_style_name(self) -> None:
        line = _make_line(
            "一、工作目标",
            style_name="Heading 1",
        )
        outline, conf = self.engine._classify_outline_level(line)
        assert outline == "heading1"
        # base + regex + style_name bonus = 0.70
        assert conf >= 0.65

    def test_style_name_heading_no_regex(self) -> None:
        line = _make_line(
            "工作目标",
            style_name="Heading 1",
            font_weight=True,
        )
        outline, conf = self.engine._classify_outline_level(line)
        assert outline == "heading1"


class TestRuleEngineHeaderClassification:
    def setup_method(self) -> None:
        self.engine = StructureRuleEngine()

    def test_issuing_number(self) -> None:
        lines = [_make_line("国办发〔2024〕1号", 0)]
        result = self.engine._classify_header(lines)
        assert len(result) == 1
        assert result[0].field == "issuing_number"
        assert result[0].confidence >= 0.70  # base+region+regex

    def test_signatory(self) -> None:
        lines = [_make_line("签发人：张三", 0)]
        result = self.engine._classify_header(lines)
        assert result[0].field == "signatory"

    def test_copy_number(self) -> None:
        lines = [_make_line("000001", 0)]
        result = self.engine._classify_header(lines)
        assert result[0].field == "copy_number"

    def test_issuing_logo(self) -> None:
        lines = [
            _make_line(
                "XX市人民政府办公室",
                0,
                font_size=26.0,
                alignment="center",
            )
        ]
        result = self.engine._classify_header(lines)
        assert result[0].field == "issuing_logo"
        assert result[0].confidence >= 0.60

    def test_urgency(self) -> None:
        lines = [_make_line("特急", 0)]
        result = self.engine._classify_header(lines)
        assert result[0].field == "urgency_level"

    def test_classification(self) -> None:
        lines = [_make_line("机密★10年", 0)]
        result = self.engine._classify_header(lines)
        assert result[0].field == "classification_duration"


class TestRuleEngineBodyClassification:
    def setup_method(self) -> None:
        self.engine = StructureRuleEngine()

    def test_title_centered_large_font(self) -> None:
        lines = [
            _make_line(
                "关于进一步加强xxx工作的通知",
                0,
                font_size=22.0,
                alignment="center",
            ),
            _make_line("正文内容", 1),
        ]
        result = self.engine._classify_body(lines)
        assert result[0].field == "title"
        assert result[0].confidence >= 0.60

    def test_addressee_after_title(self) -> None:
        lines = [
            _make_line(
                "关于xxx的通知",
                0,
                font_size=22.0,
                alignment="center",
            ),
            _make_line("各部门、各直属机构：", 1),
        ]
        result = self.engine._classify_body(lines)
        assert result[1].field == "addressee"

    def test_attachment_note(self) -> None:
        lines = [
            _make_line("正文", 0),
            _make_line("附件：关于xxx的通知", 1),
        ]
        result = self.engine._classify_body(lines)
        assert any(cl.field == "attachment_note" for cl in result)

    def test_issue_date(self) -> None:
        lines = [
            _make_line("正文", 0),
            _make_line("2024年1月15日", 1, alignment="right"),
        ]
        result = self.engine._classify_body(lines)
        assert any(cl.field == "issue_date" for cl in result)

    def test_heading1_in_body(self) -> None:
        lines = [
            _make_line(
                "一、工作目标",
                0,
                font_family="黑体",
                font_weight=True,
            ),
        ]
        result = self.engine._classify_body(lines)
        assert result[0].field == "heading1"

    def test_body_text_default(self) -> None:
        lines = [
            _make_line("这是普通的正文内容。", 0, font_family="仿宋"),
        ]
        result = self.engine._classify_body(lines)
        assert result[0].field == "body_text"


class TestRuleEngineFooterClassification:
    def setup_method(self) -> None:
        self.engine = StructureRuleEngine()

    def test_carbon_copy(self) -> None:
        lines = [_make_line("抄送：各部门。", 0)]
        result = self.engine._classify_footer(lines)
        assert result[0].field == "carbon_copy"

    def test_page_number(self) -> None:
        lines = [_make_line("- 1 -", 0)]
        result = self.engine._classify_footer(lines)
        assert result[0].field == "page_number"

    def test_issuing_office(self) -> None:
        lines = [_make_line("XX办公室    2024年1月15日印发", 0)]
        result = self.engine._classify_footer(lines)
        assert result[0].field == "issuing_office"


class TestRuleEngineFullPage:
    def setup_method(self) -> None:
        self.engine = StructureRuleEngine()

    def test_full_docx_classification(self) -> None:
        """Test classification of a complete DOCX page."""
        lines = [
            _make_line("XX市人民政府办公室", 0, font_size=26.0, alignment="center"),
            _make_line("X办发〔2024〕1号", 1),
            _make_line("关于xxx工作的通知", 2, font_size=22.0, alignment="center"),
            _make_line("各部门：", 3),
            _make_line("一、工作目标", 4, font_family="黑体", font_weight=True),
            _make_line("正文内容...", 5, font_family="仿宋"),
            _make_line("抄送：xxx。", 6),
            _make_line("XX办公室        2024年1月15日印发", 7),
        ]
        result = self.engine.classify_lines(lines, has_position=False)

        assert len(result.lines) == 9
        assert result.header_lines[0].field == "issuing_logo"
        assert result.header_lines[1].field == "issuing_number"
        assert result.body_lines[0].field == "title"
        assert result.footer_lines[0].field == "carbon_copy"
        assert result.footer_lines[1].field == "issuing_office"
        assert result.footer_lines[2].field == "distribution_date"

    def test_all_confident_for_standard_doc(self) -> None:
        lines = [
            _make_line("XX市人民政府办公室", 0, font_size=26.0, alignment="center"),
            _make_line("X办发〔2024〕1号", 1),
            _make_line("关于xxx工作的通知", 2, font_size=22.0, alignment="center"),
            _make_line("一、工作目标", 3, font_family="黑体", font_weight=True),
            _make_line("正文...", 4),
        ]
        result = self.engine.classify_lines(lines, has_position=False)
        # For a standard document, most fields should be confident
        confident_count = sum(1 for cl in result.lines if cl.confidence >= 0.5)
        assert confident_count >= 4  # at least 4 out of 5 lines confident


# ---------------------------------------------------------------------------
# OCR-resilient pattern tests
# ---------------------------------------------------------------------------


class TestOcrResilientPatterns:
    @pytest.mark.parametrize(
        "text",
        [
            " 国办发〔 2024 〕 1号 ",
            "X委〔2023〕15号",
            " 粤府办( 2024 )100号 ",
        ],
    )
    def test_issuing_number_ocr_matches(self, text: str) -> None:
        assert ISSUING_NUMBER_PATTERN_OCR.match(text), f"OCR pattern should match: {text!r}"

    @pytest.mark.parametrize(
        "text",
        [
            "2024年1月15日",
            " 2024年12月1日 ",
            "  2023年5月31日  ",
        ],
    )
    def test_issue_date_ocr_matches(self, text: str) -> None:
        assert ISSUE_DATE_PATTERN_OCR.match(text), f"OCR pattern should match: {text!r}"

    @pytest.mark.parametrize(
        "text",
        [
            " 一、工作目标",
            "  二、基本原则",
        ],
    )
    def test_heading1_ocr_matches(self, text: str) -> None:
        assert HEADING1_PATTERN_OCR.match(text), f"OCR heading1 should match: {text!r}"


# ---------------------------------------------------------------------------
# Scanned PDF classification tests
# ---------------------------------------------------------------------------


class TestRuleEngineScannedPDF:
    """Tests for scanned PDF scenarios where font metadata is absent or unreliable.

    Scanned PDFs have:
    - No font_family (empty string)
    - Estimated font_size from OCR bounding boxes (often 18-21pt instead of 22pt)
    - Alignment from bounding box position (unreliable for short text)
    - Extra whitespace from OCR artifacts
    """

    def setup_method(self) -> None:
        self.engine = StructureRuleEngine()

    def _scanned_line(
        self,
        text: str,
        line_no: int = 0,
        **kwargs: object,
    ) -> dict[str, object]:
        """Create a line dict simulating scanned PDF (no font_family)."""
        defaults = {"font_family": ""}
        defaults.update(kwargs)
        return _make_line(text, line_no, **defaults)

    # -- Issuing logo with OCR-underestimated font size --

    def test_issuing_logo_scanned_font_size_18(self) -> None:
        """OCR often underestimates issuing_logo font size to ~18pt."""
        lines = [
            self._scanned_line(
                "XX市人民政府办公室",
                0,
                font_size=18.0,
                alignment="center",
            )
        ]
        result = self.engine._classify_header(lines)
        assert result[0].field == "issuing_logo"

    def test_issuing_logo_scanned_font_size_20(self) -> None:
        """Font size 20pt with no font_family should pass via large_relative_factor."""
        lines = [
            self._scanned_line(
                "XX市人民政府办公室",
                0,
                font_size=20.0,
                alignment="center",
            )
        ]
        result = self.engine._classify_header(lines)
        assert result[0].field == "issuing_logo"

    # -- Title with OCR font size --

    def test_title_scanned_font_size_19(self) -> None:
        """OCR-estimated title font of 19pt should still be detected."""
        lines = [
            self._scanned_line(
                "关于进一步加强工作的通知",
                0,
                font_size=19.0,
                alignment="center",
            ),
            self._scanned_line("正文内容", 1),
        ]
        result = self.engine._classify_body(lines)
        assert result[0].field == "title"

    # -- Heading with OCR leading whitespace --

    def test_heading_with_leading_space(self) -> None:
        """OCR may introduce leading whitespace before heading numbers."""
        line = self._scanned_line(" 一、工作目标")
        outline, conf = self.engine._classify_outline_level(line)
        assert outline == "heading1"
        assert conf >= 0.55

    def test_heading2_with_ocr_whitespace(self) -> None:
        """OCR may add spaces around brackets in heading2."""
        line = self._scanned_line("（ 一 ）基本原则")
        outline, conf = self.engine._classify_outline_level(line)
        assert outline == "heading2"

    # -- Issuing number with OCR artifacts --

    def test_issuing_number_ocr_spaces(self) -> None:
        """OCR may introduce spaces around brackets in issuing number."""
        lines = [self._scanned_line(" 国办发〔 2024 〕 1号 ")]
        result = self.engine._classify_header(lines)
        assert result[0].field == "issuing_number"

    # -- Issue date with OCR whitespace --

    def test_issue_date_trailing_space(self) -> None:
        """OCR may add trailing whitespace to dates."""
        lines = [
            self._scanned_line("正文", 0),
            self._scanned_line(" 2024年1月15日 ", 1, alignment="right"),
        ]
        result = self.engine._classify_body(lines)
        assert any(cl.field == "issue_date" for cl in result)

    # -- Header field confidence without font info --

    def test_header_regex_confidence_without_font(self) -> None:
        """Header fields matched by regex in header region should reach confidence
        threshold even without font info (base + region + signal_agreement + regex)."""
        lines = [self._scanned_line("X办发〔2024〕1号", 0)]
        result = self.engine._classify_header(lines)
        assert result[0].field == "issuing_number"
        expected = 0.20 + 0.10 + CONFIDENCE_SIGNAL_AGREEMENT + 0.40
        assert result[0].confidence >= expected
        assert result[0].confidence >= DEFAULT_CONFIDENCE_THRESHOLD

    def test_copy_number_confidence_without_font(self) -> None:
        lines = [self._scanned_line("000001", 0)]
        result = self.engine._classify_header(lines)
        assert result[0].field == "copy_number"
        assert result[0].confidence >= DEFAULT_CONFIDENCE_THRESHOLD

    # -- Issuing signature with left alignment (scanned) --

    def test_issuing_signature_scanned_left_aligned(self) -> None:
        """Scanned PDF: short signature text near body end may get 'left' alignment."""
        lines = [
            self._scanned_line("正文内容...", 0),
            self._scanned_line("更多正文...", 1),
            self._scanned_line("XX市人民政府", 2, alignment="left"),
            self._scanned_line("2024年1月15日", 3, alignment="right"),
        ]
        result = self.engine._classify_body(lines)
        sig_lines = [cl for cl in result if cl.field == "issuing_signature"]
        assert len(sig_lines) >= 1

    # -- Full scanned page classification --

    def test_full_scanned_page_classification(self) -> None:
        """End-to-end test simulating a full scanned PDF page."""
        lines = [
            self._scanned_line(
                "XX市人民政府办公室",
                0,
                font_size=20.0,
                alignment="center",
                y0=20,
                y1=50,
            ),
            self._scanned_line("X办发〔2024〕1号", 1, y0=55, y1=70),
            self._scanned_line(
                "关于xxx工作的通知",
                2,
                font_size=19.0,
                alignment="center",
                y0=200,
                y1=225,
            ),
            self._scanned_line("各部门：", 3, y0=230, y1=245),
            self._scanned_line("一、工作目标", 4, y0=250, y1=265),
            self._scanned_line("正文内容...", 5, y0=270, y1=285),
            self._scanned_line("抄送：xxx。", 6, y0=720, y1=735),
            self._scanned_line("XX办公室  2024年1月15日印发", 7, y0=750, y1=765),
        ]
        result = self.engine.classify_lines(lines, has_position=True, page_height=841.89)

        fields = {cl.field for cl in result.lines}
        assert "issuing_logo" in fields
        assert "issuing_number" in fields
        assert "carbon_copy" in fields
        assert "issuing_office" in fields


# ---------------------------------------------------------------------------
# Heading vs body_text downgrade tests (for long paragraphs with heading prefix)
# ---------------------------------------------------------------------------


class TestHeadingBodyTextDowngrade:
    """Tests for the logic that downgrades long heading-like paragraphs to body_text.

    When a paragraph starts with a heading pattern (e.g. "（一）" or "1.") but
    contains a full sentence (has "。") and is longer than 40 chars, it should be
    treated as body_text rather than a heading, especially when the font does not
    match the expected heading font.
    """

    def setup_method(self) -> None:
        self.engine = StructureRuleEngine()

    # -- heading2: "（一）" --

    def test_heading2_long_body_paragraph_downgrade(self) -> None:
        """（一）+ long body text ( FangSong, no bold ) → should downgrade to body_text."""
        line = _make_line(
            "（一）压实党政领导责任。各级政府应当高度重视，切实加强组织领导，确保各项工作落到实处。",
            font_family="仿宋",
            font_weight=False,
        )
        outline, conf = self.engine._classify_outline_level(line)
        assert (
            outline == ""
        ), f"Expected empty (downgraded to body_text), got {outline!r} with conf={conf}"

    def test_heading2_long_body_paragraph_in_classify_body(self) -> None:
        """Via _classify_body, the same line should become body_text, not heading2."""
        lines = [
            _make_line(
                "（一）压实党政领导责任。各级政府应当高度重视，切实加强组织领导。",
                font_family="仿宋",
                font_weight=False,
            ),
        ]
        result = self.engine._classify_body(lines)
        assert result[0].field == "body_text", f"Expected body_text, got {result[0].field!r}"

    def test_heading2_long_heading_with_correct_font_kept(self) -> None:
        """（一）+ long text but correct KaiTi+bold → still heading2."""
        line = _make_line(
            "（一）压实党政领导责任。各级政府应当高度重视，切实加强组织领导，确保各项工作落到实处。",
            font_family="楷体",
            font_weight=True,
        )
        outline, conf = self.engine._classify_outline_level(line)
        assert (
            outline == "heading2"
        ), f"Expected heading2 (font matches), got {outline!r} with conf={conf}"

    def test_heading2_short_with_period_kept(self) -> None:
        """Short （一）paragraph (< 40 chars) with period → still heading2."""
        line = _make_line(
            "（一）压实党政领导责任。",
            font_family="仿宋",
            font_weight=False,
        )
        outline, conf = self.engine._classify_outline_level(line)
        assert (
            outline == "heading2"
        ), f"Expected heading2 (short text), got {outline!r} with conf={conf}"

    def test_heading2_long_without_period_kept(self) -> None:
        """Long （一）paragraph without period → still heading2."""
        line = _make_line(
            "（一）压实党政领导责任，强化监督检查，确保各项工作落到实处",
            font_family="仿宋",
            font_weight=False,
        )
        outline, conf = self.engine._classify_outline_level(line)
        assert (
            outline == "heading2"
        ), f"Expected heading2 (no period), got {outline!r} with conf={conf}"

    # -- heading3: "1." --

    def test_heading3_long_body_paragraph_downgrade(self) -> None:
        """1. + long body text ( FangSong, no bold ) → should downgrade to body_text."""
        line = _make_line(
            "1.加强组织领导。各级政府应当高度重视，切实加强组织领导，确保各项工作落到实处。",
            font_family="仿宋",
            font_weight=False,
        )
        outline, conf = self.engine._classify_outline_level(line)
        assert outline == "", f"Expected empty (downgraded), got {outline!r} with conf={conf}"

    def test_heading3_short_title_kept(self) -> None:
        """Short 1. title (bold, FangSong) → still heading3."""
        line = _make_line(
            "1.加强组织领导",
            font_family="仿宋",
            font_weight=True,
        )
        outline, conf = self.engine._classify_outline_level(line)
        assert outline == "heading3", f"Expected heading3, got {outline!r} with conf={conf}"

    # -- heading4: "（1）" --

    def test_heading4_long_body_paragraph_downgrade(self) -> None:
        """（1）+ long body text ( FangSong, no bold ) → should downgrade to body_text."""
        line = _make_line(
            "（1）加强宣传教育。各部门应当广泛开展宣传活动，切实提高公众知晓率。",
            font_family="仿宋",
            font_weight=False,
        )
        outline, conf = self.engine._classify_outline_level(line)
        assert outline == "", f"Expected empty (downgraded), got {outline!r} with conf={conf}"

    def test_heading4_short_title_kept(self) -> None:
        """Short （1）title (FangSong, no bold) → still heading4."""
        line = _make_line(
            "（1）加强宣传教育",
            font_family="仿宋",
            font_weight=False,
        )
        outline, conf = self.engine._classify_outline_level(line)
        assert outline == "heading4", f"Expected heading4, got {outline!r} with conf={conf}"

    # -- heading1: "一、" (not affected because heading1 uses Heiti and is rarely mixed) --

    def test_heading1_long_body_paragraph_downgrade(self) -> None:
        """一、+ long body text (Heiti, bold) → still heading1 (font correct, high conf)."""
        line = _make_line(
            "一、加强组织领导。各级政府应当高度重视，切实加强组织领导，确保各项工作落到实处。",
            font_family="黑体",
            font_weight=True,
        )
        outline, conf = self.engine._classify_outline_level(line)
        assert (
            outline == "heading1"
        ), f"Expected heading1 (font correct, high conf), got {outline!r} with conf={conf}"

    def test_heading1_long_body_paragraph_wrong_font_downgrade(self) -> None:
        """一、+ long body text (FangSong, no bold) → should downgrade to body_text."""
        line = _make_line(
            "一、加强组织领导。各级政府应当高度重视，切实加强组织领导，确保各项工作落到实处。",
            font_family="仿宋",
            font_weight=False,
        )
        outline, conf = self.engine._classify_outline_level(line)
        assert outline == "", f"Expected empty (downgraded), got {outline!r} with conf={conf}"
