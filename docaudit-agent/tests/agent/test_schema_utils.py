"""Tests for schema_utils — field path extraction and schema merging."""

from __future__ import annotations

from courtier.agent.core.schema_utils import extract_field_paths, merge_schema


class TestExtractFieldPaths:
    def test_flat_object(self):
        data = {"name": "张三", "age": 30, "active": True}
        result = extract_field_paths(data)
        assert result == {
            "name": "str",
            "age": "int",
            "active": "bool",
        }

    def test_nested_object(self):
        data = {"metadata": {"author": "张三", "doc_id": "D001"}}
        result = extract_field_paths(data)
        assert result == {
            "metadata.author": "str",
            "metadata.doc_id": "str",
        }

    def test_array_of_objects(self):
        data = {"pages": [{"page_no": 1, "header": {"text": "第一章"}}]}
        result = extract_field_paths(data)
        assert result == {
            "pages[].page_no": "int",
            "pages[].header.text": "str",
        }

    def test_nested_arrays(self):
        data = {"pages": [{"body": [{"text": "段落1", "font_size": 12}]}]}
        result = extract_field_paths(data)
        assert result == {
            "pages[].body[].text": "str",
            "pages[].body[].font_size": "int",
        }

    def test_null_value(self):
        data = {"name": None, "count": 5}
        result = extract_field_paths(data)
        assert result == {"name": "null", "count": "int"}

    def test_float_value(self):
        data = {"ratio": 0.95}
        result = extract_field_paths(data)
        assert result["ratio"] == "float"

    def test_empty_dict(self):
        data = {}
        result = extract_field_paths(data)
        assert result == {}

    def test_empty_array(self):
        data = {"items": []}
        result = extract_field_paths(data)
        assert result == {"items[]": "empty_array"}


class TestMergeSchema:
    def test_all_base_fields_in_actual(self):
        base = {"properties": {"metadata": {"type": "object"}}}
        actual = {"metadata.author": "str", "metadata.doc_id": "str"}
        result = merge_schema(base, actual)
        assert result["metadata.author"] == "str"
        assert result["metadata.doc_id"] == "str"

    def test_dynamic_fields_added(self):
        """Fields in actual but not in base are marked with + prefix."""
        base = {"properties": {"name": {"type": "str"}}}
        actual = {"name": "str", "extra_field": "str", "count": "int"}
        result = merge_schema(base, actual)
        assert result["name"] == "str"
        assert result["+extra_field"] == "str"
        assert result["+count"] == "int"

    def test_base_none_returns_actual_only(self):
        """When base schema is None, return actual fields as-is (no +/- marking)."""
        actual = {"name": "str", "count": "int"}
        result = merge_schema(None, actual)
        assert result == actual

    def test_no_actual_no_base(self):
        result = merge_schema(None, {})
        assert result == {}
