"""Tests for ModelClient."""

import pytest

from courtier.agent.core.model import (
    ToolCall,
    _normalize_response,
    _parse_tool_arguments,
)
from courtier.agent.telemetry.metrics import MODEL_TOOL_ARG_REPAIR_TOTAL
from courtier.agent.testing import MockModelClient


def _repair_count(tier: str) -> float:
    return MODEL_TOOL_ARG_REPAIR_TOTAL.labels(tier=tier)._value.get()


class TestParseToolArguments:
    def test_standard_json(self):
        assert _parse_tool_arguments('{"file_path": "/tmp/test.docx"}') == {
            "file_path": "/tmp/test.docx"
        }

    def test_single_quoted_dict_fallback(self):
        assert _parse_tool_arguments("{'file_path': '/tmp/test.docx'}") == {
            "file_path": "/tmp/test.docx"
        }

    def test_invalid_returns_parse_error(self, caplog):
        result = _parse_tool_arguments("not valid at all")
        assert result["_parse_error"] is True
        assert "Failed to parse tool call arguments" in caplog.text

    def test_literal_eval_non_dict_returns_parse_error(self, caplog):
        result = _parse_tool_arguments("[1, 2, 3]")
        assert result["_parse_error"] is True
        assert result["raw"] == "[1, 2, 3]"

    def test_bare_ref_inside_string_value(self):
        """Bare $ref inside a JSON string gets quoted and then fixed."""
        raw = '{"task": "使用 $ref:parse_document:1 作为输入。"}'
        result = _parse_tool_arguments(raw)
        assert result["task"] == '使用 "$ref:parse_document:1" 作为输入。'

    def test_unescaped_quotes_around_ref_inside_string(self):
        """Model emits unescaped quotes around $ref inside a larger string."""
        raw = '{"task": "使用 "$ref:parse_document:1" 作为输入。"}'
        result = _parse_tool_arguments(raw)
        assert result["task"] == '使用 "$ref:parse_document:1" 作为输入。'

    def test_bare_ref_as_top_level_value(self):
        """Bare $ref as a JSON value gets quoted into a string."""
        raw = '{"document": $ref:parse_document:1}'
        result = _parse_tool_arguments(raw)
        assert result["document"] == "$ref:parse_document:1"

    def test_already_quoted_ref_unchanged(self):
        """Properly quoted $ref is parsed as-is."""
        raw = '{"document": "$ref:parse_document:1"}'
        result = _parse_tool_arguments(raw)
        assert result["document"] == "$ref:parse_document:1"

    def test_multiple_bare_refs_in_array(self):
        """Multiple bare $ref values in a JSON array get quoted."""
        raw = '{"refs": [$ref:parse_document:1, $ref:parse_document:2]}'
        result = _parse_tool_arguments(raw)
        assert result["refs"] == ["$ref:parse_document:1", "$ref:parse_document:2"]

    def test_mixed_quote_styles_not_repaired(self):
        """Mixed single/double quotes fall through to _parse_error —
        swapping quotes would corrupt the double-quoted segments."""
        result = _parse_tool_arguments('''{"a": 1, 'b': 2}''')
        assert result["_parse_error"] is True

    def test_apostrophe_in_double_quoted_value_preserved(self):
        result = _parse_tool_arguments('{"text": "it\'s fine"}')
        assert result == {"text": "it's fine"}

    def test_trailing_comma_repaired(self):
        assert _parse_tool_arguments('{"a": 1,}') == {"a": 1}

    def test_unquoted_keys_repaired(self):
        assert _parse_tool_arguments("{a: 1}") == {"a": 1}

    def test_escaped_quote_in_single_quoted_style_returns_parse_error(self):
        result = _parse_tool_arguments(r"{'text': 'it\'s ok'}")
        assert result["_parse_error"] is True


class TestToolArgRepairMetrics:
    """_parse_tool_arguments/_normalize_response record repair tiers."""

    def test_ref_quote_fix_recorded(self):
        before = _repair_count("ref_quote_fix")
        _parse_tool_arguments('{"task": "使用 "$ref:parse_document:1" 作为输入。"}')
        assert _repair_count("ref_quote_fix") == before + 1

    def test_json_repair_recorded(self):
        before = _repair_count("json_repair")
        _parse_tool_arguments("{a: 1}")
        assert _repair_count("json_repair") == before + 1

    def test_parse_error_recorded(self):
        before = _repair_count("parse_error")
        _parse_tool_arguments("not valid at all")
        assert _repair_count("parse_error") == before + 1

    def test_xml_fallback_recorded(self):
        content = (
            "<tool_call><function=get_weather>"
            "<parameter=city>北京</parameter>"
            "</function></tool_call>"
        )
        before = _repair_count("xml_fallback")
        response = _normalize_response(
            content=content,
            tool_calls=[],
            reasoning=None,
            finish_reason="stop",
            usage=None,
            raw=None,
        )
        assert len(response.tool_calls) == 1
        assert _repair_count("xml_fallback") == before + 1


class TestMockModelClient:
    @pytest.mark.asyncio
    async def test_first_call_returns_tool_calls(self):
        tc = ToolCall(id="1", name="echo", arguments={"text": "hello"})
        client = MockModelClient(tool_calls=[tc])

        response = await client.generate([{"role": "user", "content": "echo hello"}])
        assert len(response.tool_calls) == 1
        assert response.tool_calls[0].name == "echo"
        assert response.tool_calls[0].arguments == {"text": "hello"}

    @pytest.mark.asyncio
    async def test_subsequent_call_returns_content(self):
        tc = ToolCall(id="1", name="echo", arguments={"text": "hello"})
        client = MockModelClient(tool_calls=[tc])

        await client.generate([{"role": "user", "content": "hi"}])
        response = await client.generate([{"role": "user", "content": "hi"}])
        assert response.content == "Done."
        assert len(response.tool_calls) == 0

    @pytest.mark.asyncio
    async def test_no_tool_calls_returns_content_directly(self):
        client = MockModelClient()

        response = await client.generate([{"role": "user", "content": "hi"}])
        assert response.content == "Done."
