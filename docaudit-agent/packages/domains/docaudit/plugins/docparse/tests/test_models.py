"""Tests for docmodels data models and bug fixes."""

from docmodels import (
    Body,
    Document,
    Font,
    Footer,
    Header,
    Margin,
    MetaData,
    Page,
    PageContent,
    Paragraph,
    Position,
)


class TestBugFixes:
    """Verify the three bugs in docmodels.py were fixed."""

    def test_outline_level_literal_has_separate_heading4_and_heading5(self):
        """Bug fix: missing comma caused 'heading4heading5' concatenation."""
        ann = Paragraph.model_fields["outline_level"].annotation
        # The Literal should contain 'heading4' and 'heading5' as separate values
        import typing

        args = typing.get_args(ann)
        assert "heading4" in args, f"heading4 not in Literal args: {args}"
        assert "heading5" in args, f"heading5 not in Literal args: {args}"
        assert "heading4heading5" not in args, f"concatenated value found: {args}"

    def test_body_title_field_not_tilte(self):
        """Bug fix: Body.tilte -> Body.title."""
        assert "title" in Body.model_fields, "Body should have 'title' field"
        assert "tilte" not in Body.model_fields, "Body should NOT have 'tilte' field"

    def test_metadata_position_not_postion(self):
        """Bug fix: MetaData.postion -> MetaData.position."""
        assert "position" in MetaData.model_fields, (
            "MetaData should have 'position' field"
        )
        assert "postion" not in MetaData.model_fields, (
            "MetaData should NOT have 'postion' field"
        )


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

    def test_metadata_defaults(self):
        meta = MetaData()
        assert meta.exist is False
        assert isinstance(meta.position, Position)
        assert isinstance(meta.font, Font)

    def test_paragraph_defaults(self):
        para = Paragraph()
        assert para.space_before == 0.0
        assert para.space_after == 0.0
        assert para.line_spacing == 0.0
        assert para.first_indent == 0.0
        assert para.left_indent == 0.0
        assert para.right_indent == 0.0
        assert para.outline_level == "others"
        assert para.elements == []

    def test_paragraph_with_elements(self):
        meta = MetaData(exist=True, font=Font(text="test"))
        para = Paragraph(elements=[meta], outline_level="heading1")
        assert len(para.elements) == 1
        assert para.outline_level == "heading1"

    def test_margin_defaults(self):
        margin = Margin()
        assert margin.top_margin == 0.0

    def test_header_defaults(self):
        header = Header()
        assert header.copy_number is None
        assert isinstance(header.issuing_logo, Paragraph)

    def test_body_defaults(self):
        body = Body()
        assert isinstance(body.title, Paragraph)
        assert body.main_text == []
        assert body.attachment_note is None

    def test_footer_defaults(self):
        footer = Footer()
        assert isinstance(footer.issuing_office, Paragraph)
        assert footer.carbon_copy is None

    def test_page_defaults(self):
        page = Page()
        assert page.raw == b""
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
