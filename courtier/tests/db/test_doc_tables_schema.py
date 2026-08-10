"""ORM 表结构与 docmodels 契约对齐的元数据断言（不需要真实数据库）。"""

from __future__ import annotations

from sqlalchemy import JSON

from courtier.db._utils import SECTION_FIELDS
from courtier.db.tables import (
    DocumentCreate,
    DocumentTable,
    DocumentUpdate,
    ElementTable,
    PageCreate,
    PageTable,
    PageUpdate,
    ParagraphTable,
)


class TestParagraphTable:
    def test_block_no_dropped(self):
        assert "block_no" not in ParagraphTable.__table__.columns

    def test_spacing_columns_nullable(self):
        cols = ParagraphTable.__table__.columns
        for name in (
            "space_before",
            "space_after",
            "line_spacing",
            "first_indent",
            "left_indent",
            "right_indent",
        ):
            assert cols[name].nullable, f"{name} should be nullable"

    def test_alignment_column_added(self):
        col = ParagraphTable.__table__.columns["alignment"]
        assert col.nullable
        assert col.type.length == 16

    def test_outline_level_enum_unchanged(self):
        col = ParagraphTable.__table__.columns["outline_level"]
        assert set(col.type.enums) == {
            "heading1",
            "heading2",
            "heading3",
            "heading4",
            "heading5",
            "body_text",
            "others",
        }


class TestElementTable:
    def test_exist_dropped(self):
        assert "exist" not in ElementTable.__table__.columns


class TestPageTable:
    def test_raw_dropped(self):
        assert "raw" not in PageTable.__table__.columns

    def test_save_path_dropped(self):
        assert "save_path" not in PageTable.__table__.columns


class TestDocumentTable:
    def test_user_id_dropped(self):
        assert "user_id" not in DocumentTable.__table__.columns

    def test_doc_id_comment_sha256(self):
        col = DocumentTable.__table__.columns["doc_id"]
        assert "sha256" in col.comment

    def test_schema_version_column(self):
        col = DocumentTable.__table__.columns["schema_version"]
        assert not col.nullable
        assert col.type.length == 8
        assert col.default.arg == "1.0"

    def test_warnings_column(self):
        col = DocumentTable.__table__.columns["warnings"]
        assert col.nullable
        assert isinstance(col.type, JSON)


class TestSectionFields:
    def test_all_slots_optional(self):
        """模型全 Optional 化后，17 个槽位的 is_optional 标志全部为 True。"""
        assert len(SECTION_FIELDS) == 21
        optional_flags = {entry[3] for entry in SECTION_FIELDS}
        assert optional_flags == {True}

    def test_single_list_slot(self):
        list_slots = [entry[0] for entry in SECTION_FIELDS if entry[4]]
        assert list_slots == ["body_main_text"]


class TestPydanticSchemas:
    """pydantic Create/Update schema 与 Table 列对齐（残余字段已清除）。"""

    def test_document_create_drops_user_id_adds_new_fields(self):
        fields = DocumentCreate.model_fields
        assert "user_id" not in fields
        assert "schema_version" in fields
        assert "warnings" in fields
        # 默认值与 Table 列 default 对齐
        assert DocumentCreate(doc_id="d", total_page_num=1).schema_version == "1.0"
        assert DocumentCreate(doc_id="d", total_page_num=1).warnings is None

    def test_document_update_drops_user_id_adds_new_fields(self):
        fields = DocumentUpdate.model_fields
        assert "user_id" not in fields
        assert "schema_version" in fields
        assert "warnings" in fields

    def test_page_schemas_drop_save_path(self):
        assert "save_path" not in PageCreate.model_fields
        assert "save_path" not in PageUpdate.model_fields
