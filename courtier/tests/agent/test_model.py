"""Tests for ModelClient."""

import pytest

from courtier.agent.core.model import (
    ToolCall,
    _parse_tool_arguments,
)
from courtier.agent.testing import MockModelClient


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
