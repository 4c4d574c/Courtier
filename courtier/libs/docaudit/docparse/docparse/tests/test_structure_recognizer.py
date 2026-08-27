"""Tests for rule-engine -> PageContent conversion and spacing estimation."""

from docmodels import Header, Margin, Position
from docparse.parsers.structure_recognizer import (
    _build_paragraph,
    _merge_body_text_into_paragraphs,
    _validate_header_slots,
)

SAMPLE_LINES = [
    {
        "text": "机密★1年",
        "line_no": 0,
        "x0": 10,
        "y0": 10,
        "x1": 100,
        "y1": 30,
        "font_family": "SimSun",
        "font_size": 16.0,
        "font_weight": False,
        "font_style": False,
    },
    {
        "text": "XX人民政府办公室",
        "line_no": 1,
        "x0": 50,
        "y0": 50,
        "x1": 300,
        "y1": 80,
        "font_family": "SimSun",
        "font_size": 26.0,
        "font_weight": True,
        "font_style": False,
    },
    {
        "text": "X政办发〔2024〕1号",
        "line_no": 2,
        "x0": 80,
        "y0": 100,
        "x1": 250,
        "y1": 120,
        "font_family": "SimSun",
        "font_size": 16.0,
        "font_weight": False,
        "font_style": False,
    },
    {
        "text": "关于XXX的通知",
        "line_no": 3,
        "x0": 100,
        "y0": 200,
        "x1": 300,
        "y1": 230,
        "font_family": "SimSun",
        "font_size": 22.0,
        "font_weight": True,
        "font_style": False,
    },
    {
        "text": "各县（市、区）人民政府：",
        "line_no": 4,
        "x0": 80,
        "y0": 260,
        "x1": 350,
        "y1": 280,
        "font_family": "SimSun",
        "font_size": 16.0,
        "font_weight": False,
        "font_style": False,
    },
    {
        "text": "为贯彻落实……",
        "line_no": 5,
        "x0": 80,
        "y0": 300,
        "x1": 500,
        "y1": 320,
        "font_family": "SimSun",
        "font_size": 16.0,
        "font_weight": False,
        "font_style": False,
    },
]


class TestBuildParagraph:
    """Test _build_paragraph helper."""

    def test_with_matching_lines(self):
        para = _build_paragraph("test text", [0, 1], SAMPLE_LINES)
        assert len(para.elements) == 2
        assert para.elements[0].font.text == "机密★1年"
        assert para.elements[0].position.x0 == 10
        assert para.elements[1].font.text == "XX人民政府办公室"

    def test_with_empty_indices(self):
        """No matching lines: keep text with a default (unmeasured) position."""
        para = _build_paragraph("test", [], SAMPLE_LINES)
        assert len(para.elements) == 1
        assert para.elements[0].font.text == "test"
        assert para.elements[0].position == Position()

    def test_spacing_taken_from_first_matched_line(self):
        """DOCX lines carry paragraph spacing; it lands on the Paragraph."""
        lines = [
            dict(
                SAMPLE_LINES[0],
                space_before=12.0,
                line_spacing=28.0,
                first_indent=32.0,
                left_indent=0.0,
            )
        ]
        para = _build_paragraph("机密★1年", [0], lines)
        assert para.space_before == 12.0
        assert para.line_spacing == 28.0
        assert para.first_indent == 32.0
        assert para.left_indent == 0.0
        # Keys absent from the line stay None (未提取)
        assert para.space_after is None
        assert para.right_indent is None

    def test_lines_without_spacing_keys_stay_none(self):
        para = _build_paragraph("test", [0], SAMPLE_LINES)
        assert para.space_before is None
        assert para.line_spacing is None
        assert para.first_indent is None

    def test_with_out_of_range_indices(self):
        para = _build_paragraph("test", [0, 99], SAMPLE_LINES)
        assert len(para.elements) == 1  # Only index 0 is valid

    def test_with_custom_outline_level(self):
        para = _build_paragraph("heading", [3], SAMPLE_LINES, outline_level="heading1")
        assert para.outline_level == "heading1"


class TestValidateHeaderSlots:
    """Test _validate_header_slots: 密级槽与发文字号槽装反时纠偏。"""

    def test_swapped_slots_are_swapped_back(self):
        header = Header(
            classification_duration=_build_paragraph("X政办发〔2024〕1号", [2], SAMPLE_LINES),
            issuing_number=_build_paragraph("机密★1年", [0], SAMPLE_LINES),
        )

        _validate_header_slots(header)

        assert header.classification_duration.elements[0].font.text == "机密★1年"
        assert header.issuing_number.elements[0].font.text == "X政办发〔2024〕1号"

    def test_correct_slots_untouched(self):
        header = Header(
            classification_duration=_build_paragraph("机密★1年", [0], SAMPLE_LINES),
            issuing_number=_build_paragraph("X政办发〔2024〕1号", [2], SAMPLE_LINES),
        )

        _validate_header_slots(header)

        assert header.classification_duration.elements[0].font.text == "机密★1年"
        assert header.issuing_number.elements[0].font.text == "X政办发〔2024〕1号"

    def test_ambiguous_slots_untouched(self):
        """两侧都无明确文本特征时不做交换。"""
        header = Header(
            classification_duration=_build_paragraph("长期", [], SAMPLE_LINES),
            issuing_number=_build_paragraph("5号", [], SAMPLE_LINES),
        )

        _validate_header_slots(header)

        assert header.classification_duration.elements[0].font.text == "长期"
        assert header.issuing_number.elements[0].font.text == "5号"

class TestMergeBodyTextIntoParagraphs:
    """Test _merge_body_text_into_paragraphs helper."""

    def test_merge_consecutive_body_text_lines(self):
        """Close body_text lines are merged into one paragraph."""
        lines = [
            {"text": "第一行", "y0": 100, "y1": 120},
            {"text": "第二行", "y0": 125, "y1": 145},
            {"text": "第三行", "y0": 150, "y1": 170},
        ]
        body_main_text = [
            ("body_text", [0]),
            ("body_text", [1]),
            ("body_text", [2]),
        ]
        merged = _merge_body_text_into_paragraphs(body_main_text, lines)
        assert len(merged) == 1
        assert merged[0][0] == "body_text"
        assert set(merged[0][1]) == {0, 1, 2}

    def test_paragraph_break_detected_by_gap(self):
        """Large Y-gap splits body_text into separate paragraphs."""
        lines = [
            {"text": "第一行", "y0": 100, "y1": 120},
            {"text": "第二行", "y0": 125, "y1": 145},
            {"text": "第三行", "y0": 300, "y1": 320},
        ]
        body_main_text = [
            ("body_text", [0]),
            ("body_text", [1]),
            ("body_text", [2]),
        ]
        merged = _merge_body_text_into_paragraphs(body_main_text, lines)
        assert len(merged) == 2
        # First paragraph: lines 0, 1 (close together)
        assert set(merged[0][1]) == {0, 1}
        # Second paragraph: line 2 (far away)
        assert merged[1][1] == [2]

    def test_heading_lines_not_merged_with_body_text(self):
        """Heading lines are not merged with body_text lines."""
        lines = [
            {"text": "一、标题", "y0": 100, "y1": 120},
            {"text": "正文第一行", "y0": 125, "y1": 145},
            {"text": "正文第二行", "y0": 150, "y1": 170},
        ]
        body_main_text = [
            ("heading2", [0]),
            ("body_text", [1]),
            ("body_text", [2]),
        ]
        merged = _merge_body_text_into_paragraphs(body_main_text, lines)
        assert len(merged) == 2
        assert merged[0][0] == "heading2"
        assert merged[0][1] == [0]
        assert merged[1][0] == "body_text"
        assert set(merged[1][1]) == {1, 2}

    def test_single_line_no_merge(self):
        """Single line returns as-is."""
        lines = [{"text": "单行", "y0": 100, "y1": 120}]
        body_main_text = [("body_text", [0])]
        merged = _merge_body_text_into_paragraphs(body_main_text, lines)
        assert len(merged) == 1
        assert merged[0] == ("body_text", [0])

    def test_docx_all_zero_positions_not_merged(self):
        """DOCX 特征（所有行 position 全 0）跳过合并：每个 w:p 独立成段。

        合并是为 PDF/扫描的视觉断行设计的；DOCX 行恒 0 坐标会让 gap 恒 0，
        连续同 outline 的 Word 段被并成一段，中间段的 space_before/after
        随首行取值被吞掉。因此全 0 position 时原样返回、逐行成段。
        """
        lines = [
            {"text": "第一段", "y0": 0.0, "y1": 0.0, "space_before": None},
            {"text": "第二段", "y0": 0.0, "y1": 0.0, "space_before": 20.0},
            {"text": "第三段", "y0": 0.0, "y1": 0.0, "space_before": None},
        ]
        body_main_text = [
            ("body_text", [0]),
            ("body_text", [1]),
            ("body_text", [2]),
        ]
        merged = _merge_body_text_into_paragraphs(body_main_text, lines)
        assert merged == body_main_text

    def test_partial_zero_positions_still_merges(self):
        """只有部分行缺坐标时不触发 DOCX 短路，仍按 Y-gap 合并。"""
        lines = [
            {"text": "第一行", "y0": 100, "y1": 120},
            {"text": "第二行", "y0": 125, "y1": 145},
            {"text": "缺坐标行", "y0": 0.0, "y1": 0.0},
        ]
        body_main_text = [
            ("body_text", [0]),
            ("body_text", [1]),
            ("body_text", [2]),
        ]
        merged = _merge_body_text_into_paragraphs(body_main_text, lines)
        # 非全 0：按 gap 合并（缺坐标行 y0=0 排到最前，独立成段）
        assert len(merged) == 2


# ---------------------------------------------------------------------------
# Rule engine result conversion: unclassified fallback
# ---------------------------------------------------------------------------


def _rule_lines():
    return [
        {
            "text": "XX人民政府办公室",
            "line_no": 0,
            "x0": 50,
            "y0": 10,
            "x1": 300,
            "y1": 40,
            "font_family": "宋体",
            "font_size": 22.0,
        },
        {
            "text": "无法归类的短行",
            "line_no": 1,
            "x0": 80,
            "y0": 100,
            "x1": 200,
            "y1": 120,
            "font_family": "仿宋",
            "font_size": 16.0,
        },
        {
            "text": "另一行未分类内容",
            "line_no": 2,
            "x0": 80,
            "y0": 130,
            "x1": 220,
            "y1": 150,
            "font_family": "仿宋",
            "font_size": 16.0,
        },
    ]


class TestClassifiedLinesUnclassifiedFallback:
    """Unclassified rule-engine lines fall back to body, never dropped."""

    def _classified(self):
        from docparse.parsers.rules import ClassifiedLine

        lines = _rule_lines()
        return [
            ClassifiedLine(
                line_no=0,
                text=lines[0]["text"],
                field="issuing_logo",
                confidence=0.9,
            ),
            ClassifiedLine(
                line_no=1,
                text=lines[1]["text"],
                field="unclassified",
                confidence=0.0,
            ),
            ClassifiedLine(
                line_no=2,
                text=lines[2]["text"],
                field="unclassified",
                confidence=0.0,
            ),
        ]

    def test_unclassified_lines_land_in_body(self):
        from docparse.parsers.structure_recognizer import (
            _classified_lines_to_page_content,
        )

        lines = _rule_lines()
        warnings: list[str] = []
        content = _classified_lines_to_page_content(
            self._classified(), lines, Margin(), warnings=warnings
        )

        # Header field mapped normally
        assert content.header.issuing_logo.elements
        # Unclassified lines preserved as body text, in original order
        body_texts = [elem.font.text for para in content.body.main_text for elem in para.elements]
        assert "无法归类的短行" in body_texts
        assert "另一行未分类内容" in body_texts
        assert all(para.outline_level == "body_text" for para in content.body.main_text)
        # Original line order preserved
        assert body_texts.index("无法归类的短行") < body_texts.index("另一行未分类内容")
        # Summary warning recorded
        assert len(warnings) == 1
        assert "2 行未能自动归类" in warnings[0]

    def test_no_unclassified_no_warning(self):
        from docparse.parsers.rules import ClassifiedLine
        from docparse.parsers.structure_recognizer import (
            _classified_lines_to_page_content,
        )

        lines = _rule_lines()
        classified = [
            ClassifiedLine(
                line_no=0,
                text=lines[0]["text"],
                field="issuing_logo",
                confidence=0.9,
            ),
        ]
        warnings: list[str] = []
        _classified_lines_to_page_content(classified, lines, Margin(), warnings=warnings)
        assert warnings == []
