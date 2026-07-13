"""Tests for LLM structure recognizer."""

from unittest.mock import MagicMock

from docmodels import Body, Footer, Header, Margin, PageContent
from docparse.parsers.structure_recognizer import (
    _build_optional_paragraph,
    _build_paragraph,
    _merge_body_text_into_paragraphs,
    _parse_body,
    _parse_footer,
    _parse_header,
    recognize_page_structure,
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
        para = _build_paragraph("test", [], SAMPLE_LINES)
        assert len(para.elements) == 1
        assert para.elements[0].exist is False
        assert para.elements[0].font.text == "test"

    def test_with_out_of_range_indices(self):
        para = _build_paragraph("test", [0, 99], SAMPLE_LINES)
        assert len(para.elements) == 1  # Only index 0 is valid

    def test_with_custom_outline_level(self):
        para = _build_paragraph("heading", [3], SAMPLE_LINES, outline_level="heading1")
        assert para.outline_level == "heading1"


class TestBuildOptionalParagraph:
    """Test _build_optional_paragraph helper."""

    def test_none_data_returns_none(self):
        result = _build_optional_paragraph(None, SAMPLE_LINES)
        assert result is None

    def test_empty_data_returns_none(self):
        result = _build_optional_paragraph(
            {"text": "", "line_indices": []}, SAMPLE_LINES
        )
        assert result is None

    def test_valid_data_returns_paragraph(self):
        result = _build_optional_paragraph(
            {"text": "test", "line_indices": [0]}, SAMPLE_LINES
        )
        assert result is not None
        assert isinstance(result, type(SAMPLE_LINES)) or result is not None


class TestParseHeader:
    """Test _parse_header function."""

    def test_none_header_data(self):
        header = _parse_header(None, SAMPLE_LINES)
        assert isinstance(header, Header)

    def test_header_with_data(self):
        data = {
            "copy_number": None,
            "classification_duration": {"text": "机密★1年", "line_indices": [0]},
            "urgency_level": None,
            "issuing_logo": {"text": "XX人民政府办公室", "line_indices": [1]},
            "issuing_number": {"text": "X政办发〔2024〕1号", "line_indices": [2]},
            "signatory": {"text": "", "line_indices": []},
            "ruling_line_pos": {"text": "", "line_indices": []},
        }
        header = _parse_header(data, SAMPLE_LINES)
        assert header.classification_duration is not None
        assert header.issuing_logo.elements[0].font.text == "XX人民政府办公室"


class TestParseBody:
    """Test _parse_body function."""

    def test_none_body_data(self):
        body = _parse_body(None, SAMPLE_LINES)
        assert isinstance(body, Body)

    def test_body_with_main_text_list(self):
        data = {
            "title": {"text": "关于XXX的通知", "line_indices": [3]},
            "addressee": {"text": "各县（市、区）人民政府：", "line_indices": [4]},
            "main_text": [
                {"text": "为贯彻落实……", "line_indices": [5]},
            ],
            "attachment_note": None,
            "issuing_signature": {"text": "", "line_indices": []},
            "issue_date": {"text": "", "line_indices": []},
            "stamp": None,
            "note": None,
            "attachments": None,
        }
        body = _parse_body(data, SAMPLE_LINES)
        assert body.title.outline_level == "heading1"
        assert len(body.main_text) == 1
        assert body.main_text[0].outline_level == "body_text"

    def test_heading2_not_bold_downgraded_to_body_text(self):
        """heading2 without bold font_weight is downgraded to body_text."""
        lines = [
            {
                "text": "（一）压实党政领导责任",
                "line_no": 0,
                "font_weight": False,
                "font_family": "仿宋",
            },
        ]
        data = {
            "title": None,
            "addressee": None,
            "main_text": [
                {
                    "text": "（一）压实党政领导责任",
                    "line_indices": [0],
                    "outline_level": "heading2",
                },
            ],
            "attachment_note": None,
            "issuing_signature": None,
            "issue_date": None,
            "stamp": None,
            "note": None,
            "attachments": None,
        }
        body = _parse_body(data, lines)
        assert body.main_text[0].outline_level == "body_text"

    def test_heading2_bold_kept(self):
        """heading2 with bold font_weight stays as heading2."""
        lines = [
            {
                "text": "（一）压实党政领导责任",
                "line_no": 0,
                "font_weight": True,
                "font_family": "楷体",
            },
        ]
        data = {
            "title": None,
            "addressee": None,
            "main_text": [
                {
                    "text": "（一）压实党政领导责任",
                    "line_indices": [0],
                    "outline_level": "heading2",
                },
            ],
            "attachment_note": None,
            "issuing_signature": None,
            "issue_date": None,
            "stamp": None,
            "note": None,
            "attachments": None,
        }
        body = _parse_body(data, lines)
        assert body.main_text[0].outline_level == "heading2"

    def test_heading3_not_bold_downgraded_to_body_text(self):
        """heading3 without bold font_weight is downgraded to body_text."""
        lines = [
            {
                "text": "1.工作目标",
                "line_no": 0,
                "font_weight": False,
                "font_family": "仿宋",
            },
        ]
        data = {
            "title": None,
            "addressee": None,
            "main_text": [
                {
                    "text": "1.工作目标",
                    "line_indices": [0],
                    "outline_level": "heading3",
                },
            ],
            "attachment_note": None,
            "issuing_signature": None,
            "issue_date": None,
            "stamp": None,
            "note": None,
            "attachments": None,
        }
        body = _parse_body(data, lines)
        assert body.main_text[0].outline_level == "body_text"

    def test_heading1_not_bold_downgraded_to_body_text(self):
        """heading1 without bold font_weight is downgraded to body_text."""
        lines = [
            {
                "text": "一、工作目标",
                "line_no": 0,
                "font_weight": False,
                "font_family": "仿宋",
            },
        ]
        data = {
            "title": None,
            "addressee": None,
            "main_text": [
                {
                    "text": "一、工作目标",
                    "line_indices": [0],
                    "outline_level": "heading1",
                },
            ],
            "attachment_note": None,
            "issuing_signature": None,
            "issue_date": None,
            "stamp": None,
            "note": None,
            "attachments": None,
        }
        body = _parse_body(data, lines)
        assert body.main_text[0].outline_level == "body_text"

    def test_body_text_unaffected(self):
        """body_text paragraphs are not affected by the bold check."""
        lines = [
            {
                "text": "为贯彻落实……",
                "line_no": 0,
                "font_weight": False,
                "font_family": "仿宋",
            },
        ]
        data = {
            "title": None,
            "addressee": None,
            "main_text": [
                {
                    "text": "为贯彻落实……",
                    "line_indices": [0],
                    "outline_level": "body_text",
                },
            ],
            "attachment_note": None,
            "issuing_signature": None,
            "issue_date": None,
            "stamp": None,
            "note": None,
            "attachments": None,
        }
        body = _parse_body(data, lines)
        assert body.main_text[0].outline_level == "body_text"

    def test_heading_multi_line_any_bold_kept(self):
        """Multi-line heading keeps level if any line is bold."""
        lines = [
            {
                "text": "（一）压实党政",
                "line_no": 0,
                "font_weight": False,
                "font_family": "仿宋",
            },
            {
                "text": "领导责任",
                "line_no": 1,
                "font_weight": True,
                "font_family": "楷体",
            },
        ]
        data = {
            "title": None,
            "addressee": None,
            "main_text": [
                {
                    "text": "（一）压实党政领导责任",
                    "line_indices": [0, 1],
                    "outline_level": "heading2",
                },
            ],
            "attachment_note": None,
            "issuing_signature": None,
            "issue_date": None,
            "stamp": None,
            "note": None,
            "attachments": None,
        }
        body = _parse_body(data, lines)
        assert body.main_text[0].outline_level == "heading2"


class TestParseFooter:
    """Test _parse_footer function."""

    def test_none_footer_data(self):
        footer = _parse_footer(None, SAMPLE_LINES)
        assert isinstance(footer, Footer)


class TestRecognizePageStructure:
    """Test the recognize_page_structure function (multimodal)."""

    def test_empty_lines_returns_empty_page_content(self):
        mock_client = MagicMock()
        result = recognize_page_structure([], Margin(), mock_client, "/fake/image.png")
        assert isinstance(result, PageContent)
        mock_client.recognize_structure.assert_not_called()

    def test_with_lines_calls_llm(self):
        mock_client = MagicMock()
        mock_client.recognize_structure.return_value = {
            "header": {
                "copy_number": None,
                "classification_duration": None,
                "urgency_level": None,
                "issuing_logo": {"text": "XX人民政府办公室", "line_indices": [1]},
                "issuing_number": {"text": "X政办发〔2024〕1号", "line_indices": [2]},
                "signatory": {"text": "", "line_indices": []},
                "ruling_line_pos": {"text": "", "line_indices": []},
            },
            "body": {
                "title": {"text": "关于XXX的通知", "line_indices": [3]},
                "addressee": {"text": "各县（市、区）人民政府：", "line_indices": [4]},
                "main_text": [{"text": "为贯彻落实……", "line_indices": [5]}],
                "attachment_note": None,
                "issuing_signature": {"text": "", "line_indices": []},
                "issue_date": {"text": "", "line_indices": []},
                "stamp": None,
                "note": None,
                "attachments": None,
            },
            "footer": {
                "closing_line": {"text": "", "line_indices": []},
                "carbon_copy": None,
                "issuing_office": {"text": "", "line_indices": []},
                "distribution_date": {"text": "", "line_indices": []},
                "page_number": {"text": "", "line_indices": []},
            },
        }

        from docparse.parsers.base import ParserConfig

        config = ParserConfig()

        result = recognize_page_structure(
            SAMPLE_LINES,
            Margin(),
            mock_client,
            "/fake/image.png",
            config=config,
        )
        assert isinstance(result, PageContent)
        mock_client.recognize_structure.assert_called_once_with(
            SAMPLE_LINES, "/fake/image.png"
        )
        assert result.body.title.outline_level == "heading1"
        assert len(result.body.main_text) == 1


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
