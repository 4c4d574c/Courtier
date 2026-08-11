"""Tests for docmodels data models and bug fixes."""

import typing

import pytest
from docmodels import (
    Body,
    Document,
    Font,
    Footer,
    Header,
    LineElement,
    Margin,
    Page,
    PageContent,
    Paragraph,
    Position,
)
from pydantic import ValidationError


class TestBugFixes:
    """Verify the three bugs in docmodels.py were fixed."""

    def test_outline_level_literal_has_separate_heading4_and_heading5(self):
        """Bug fix: missing comma caused 'heading4heading5' concatenation."""
        ann = Paragraph.model_fields["outline_level"].annotation
        # The Literal should contain 'heading4' and 'heading5' as separate values
        args = typing.get_args(ann)
        assert "heading4" in args, f"heading4 not in Literal args: {args}"
        assert "heading5" in args, f"heading5 not in Literal args: {args}"
        assert "heading4heading5" not in args, f"concatenated value found: {args}"

    def test_body_title_field_not_tilte(self):
        """Bug fix: Body.tilte -> Body.title."""
        assert "title" in Body.model_fields, "Body should have 'title' field"
        assert "tilte" not in Body.model_fields, "Body should NOT have 'tilte' field"

    def test_line_element_position_not_postion(self):
        """Bug fix: MetaData.postion -> MetaData.position (now LineElement)."""
        assert "position" in LineElement.model_fields, "LineElement should have 'position' field"
        assert (
            "postion" not in LineElement.model_fields
        ), "LineElement should NOT have 'postion' field"


class TestRemovedFields:
    """Fields removed by the model refactor must be gone."""

    def test_page_raw_and_save_path_removed(self):
        assert "raw" not in Page.model_fields
        assert "save_path" not in Page.model_fields

    def test_document_user_id_removed(self):
        assert "user_id" not in Document.model_fields

    def test_line_element_exist_removed(self):
        assert "exist" not in LineElement.model_fields

    def test_paragraph_block_no_removed(self):
        assert "block_no" not in Paragraph.model_fields

    def test_outline_level_drops_line_values(self):
        """ruling_line/closing_line have no producer and are not in the DB enum."""
        args = typing.get_args(Paragraph.model_fields["outline_level"].annotation)
        assert set(args) == {
            "heading1",
            "heading2",
            "heading3",
            "heading4",
            "heading5",
            "body_text",
            "others",
        }

    def test_legacy_payload_fields_ignored(self):
        """Old caches with deleted fields still validate (extra='ignore')."""
        payload = {
            "user_id": "u",
            "doc_id": "d",
            "pages": [
                {
                    "raw": "QUJD",
                    "save_path": "/host/page",
                    "page_no": 0,
                    "page_content": {
                        "body": {
                            "main_text": [
                                {
                                    "block_no": 3,
                                    "elements": [{"exist": True, "font": {"text": "x"}}],
                                }
                            ]
                        }
                    },
                }
            ],
        }
        doc = Document.model_validate(payload)
        assert doc.doc_id == "d"
        assert not hasattr(doc, "user_id")
        assert not hasattr(doc.pages[0], "raw")
        # total_page_num backfilled from pages
        assert doc.total_page_num == 1


class TestModelConstraints:
    """Field constraints added by the model refactor."""

    def test_page_no_must_be_non_negative(self):
        with pytest.raises(ValidationError):
            Page(page_no=-1)

    def test_font_size_must_be_non_negative(self):
        with pytest.raises(ValidationError):
            Font(font_size=-0.5)

    @pytest.mark.parametrize(
        "field", ["top_margin", "bottom_margin", "left_margin", "right_margin"]
    )
    def test_margin_must_be_non_negative(self, field: str):
        with pytest.raises(ValidationError):
            Margin(**{field: -1.0})

    def test_schema_version_default(self):
        assert Document().schema_version == "1.0"

    def test_source_default_empty(self):
        """source 默认空串（未知）；schema_version 保持 1.0 纯增量。"""
        doc = Document()
        assert doc.source == ""
        assert doc.schema_version == "1.0"

    def test_source_serialization_roundtrip(self):
        doc = Document(doc_id="d", source="pdf")
        restored = Document.model_validate_json(doc.model_dump_json())
        assert restored.source == "pdf"

    def test_old_payload_without_source_deserializes(self):
        """旧缓存（无 source 字段）反序列化兼容，source 落默认值。"""
        doc = Document.model_validate({"doc_id": "d", "total_page_num": 1, "pages": []})
        assert doc.source == ""

    def test_total_page_num_backfilled_from_pages(self):
        doc = Document(doc_id="d", pages=[Page(page_no=0), Page(page_no=1)])
        assert doc.total_page_num == 2

    def test_total_page_num_explicit_value_kept(self):
        """Logical page count may differ from len(pages); never overwritten."""
        doc = Document(doc_id="d", total_page_num=5, pages=[Page(page_no=0)])
        assert doc.total_page_num == 5

    def test_total_page_num_stays_zero_without_pages(self):
        assert Document(doc_id="d").total_page_num == 0


class TestModelCreation:
    """Test creating model instances with default values."""

    def test_position_defaults(self):
        pos = Position()
        assert pos.x0 == 0.0
        assert pos.y0 == 0.0
        assert pos.x1 == 0.0
        assert pos.y1 == 0.0

    def test_font_defaults(self):
        font = Font()
        assert font.font_family == ""
        assert font.font_size == 0.0
        assert font.font_weight is False
        assert font.font_style is False
        assert font.text == ""
        assert font.line_no == 0

    def test_line_element_defaults(self):
        elem = LineElement()
        assert isinstance(elem.position, Position)
        assert isinstance(elem.font, Font)

    def test_paragraph_spacing_defaults_to_none(self):
        """None = 未提取, distinct from a measured 0.0."""
        para = Paragraph()
        assert para.space_before is None
        assert para.space_after is None
        assert para.line_spacing is None
        assert para.first_indent is None
        assert para.left_indent is None
        assert para.right_indent is None
        assert para.outline_level == "others"
        assert para.elements == []

    def test_paragraph_with_elements(self):
        elem = LineElement(font=Font(text="test"))
        para = Paragraph(elements=[elem], outline_level="heading1")
        assert len(para.elements) == 1
        assert para.outline_level == "heading1"

    def test_margin_defaults(self):
        margin = Margin()
        assert margin.top_margin == 0.0

    def test_header_slots_default_to_none(self):
        header = Header()
        assert header.copy_number is None
        assert header.classification_duration is None
        assert header.urgency_level is None
        assert header.issuing_logo is None
        assert header.issuing_number is None
        assert header.signatory is None
        assert header.ruling_line_pos is None

    def test_body_slots_default_to_none(self):
        body = Body()
        assert body.title is None
        assert body.addressee is None
        assert body.main_text == []
        assert body.attachment_note is None
        assert body.issuing_signature is None
        assert body.issue_date is None
        assert body.stamp is None
        assert body.note is None
        assert body.attachments is None

    def test_footer_slots_default_to_none(self):
        footer = Footer()
        assert footer.closing_line is None
        assert footer.carbon_copy is None
        assert footer.issuing_office is None
        assert footer.distribution_date is None
        assert footer.page_number is None

    def test_page_defaults(self):
        page = Page()
        assert page.page_no == 0
        assert isinstance(page.page_content, PageContent)

    def test_document_defaults(self):
        doc = Document()
        assert doc.doc_id == ""
        assert doc.total_page_num == 0
        assert doc.pages == []

    def test_document_with_pages(self):
        page = Page(page_no=0)
        doc = Document(
            doc_id="abc123",
            total_page_num=1,
            pages=[page],
        )
        assert doc.doc_id == "abc123"
        assert len(doc.pages) == 1


class TestModelSerialization:
    """Test model JSON serialization."""

    def test_document_to_json(self):
        doc = Document(doc_id="test123", total_page_num=1)
        json_str = doc.model_dump_json()
        assert "test123" in json_str
        assert "schema_version" in json_str

    def test_document_from_json(self):
        doc = Document(doc_id="test123", total_page_num=1)
        json_str = doc.model_dump_json()
        restored = Document.model_validate_json(json_str)
        assert restored.doc_id == "test123"

    def test_paragraph_with_indents(self):
        """Left and right indent are stored and serialized correctly."""
        para = Paragraph(left_indent=32.0, right_indent=16.0)
        assert para.left_indent == 32.0
        assert para.right_indent == 16.0
        json_str = para.model_dump_json()
        assert "left_indent" in json_str
        assert "right_indent" in json_str

    def test_paragraph_spacing_none_round_trip(self):
        para = Paragraph.model_validate_json(Paragraph().model_dump_json())
        assert para.space_before is None
        assert para.line_spacing is None


class TestStarImport:
    """`from docmodels import *` exposes LineElement, not MetaData."""

    def test_line_element_exported(self):
        import docmodels

        assert "LineElement" in docmodels.__all__
        assert "MetaData" not in docmodels.__all__
        assert hasattr(docmodels, "LineElement")
        assert not hasattr(docmodels, "MetaData")

    def test_star_import_namespace(self):
        namespace: dict = {}
        exec("from docmodels import *", namespace)  # noqa: S102 - test intent
        assert "LineElement" in namespace
        assert "MetaData" not in namespace
