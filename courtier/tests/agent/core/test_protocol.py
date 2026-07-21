"""Tests for the canonical OpenAI wire conversion pair in protocol.py."""

import json

from courtier.agent.core.protocol import (
    ChatMessage,
    ToolCall,
    from_openai_dict,
    to_openai_dict,
)
from courtier.agent.core.state import Message


class TestToOpenaiDict:
    def test_plain_message(self):
        msg = ChatMessage(role="user", content="你好")
        assert to_openai_dict(msg) == {"role": "user", "content": "你好"}

    def test_content_none_omitted(self):
        assert "content" not in to_openai_dict(ChatMessage(role="assistant"))

    def test_tool_calls_arguments_serialized_to_json_string(self):
        msg = ChatMessage(
            role="assistant",
            tool_calls=[ToolCall(id="c1", name="echo", arguments={"text": "hi", "n": 1})],
        )
        d = to_openai_dict(msg)
        tool_call = d["tool_calls"][0]
        assert tool_call["id"] == "c1"
        assert tool_call["type"] == "function"
        assert tool_call["function"]["name"] == "echo"
        arguments = tool_call["function"]["arguments"]
        assert isinstance(arguments, str)
        assert json.loads(arguments) == {"text": "hi", "n": 1}

    def test_arguments_serialized_without_ascii_escaping(self):
        msg = ChatMessage(
            role="assistant",
            tool_calls=[ToolCall(id="c1", name="echo", arguments={"text": "你好"})],
        )
        arguments = to_openai_dict(msg)["tool_calls"][0]["function"]["arguments"]
        assert "你好" in arguments  # ensure_ascii=False

    def test_tool_call_id_and_name_emitted(self):
        msg = ChatMessage(role="tool", content="ok", tool_call_id="c1", name="echo")
        d = to_openai_dict(msg)
        assert d["tool_call_id"] == "c1"
        assert d["name"] == "echo"

    def test_reasoning_content_never_emitted(self):
        msg = ChatMessage(role="assistant", content="hi", reasoning_content="internal")
        assert "reasoning_content" not in to_openai_dict(msg)


class TestFromOpenaiDict:
    def _wire_tool_call(self, arguments: object) -> dict:
        return {
            "id": "c1",
            "type": "function",
            "function": {"name": "echo", "arguments": arguments},
        }

    def test_string_arguments_parsed_to_dict(self):
        d = {
            "role": "assistant",
            "tool_calls": [self._wire_tool_call('{"text": "hi"}')],
        }
        msg = from_openai_dict(d)
        assert msg.tool_calls is not None
        assert msg.tool_calls[0].id == "c1"
        assert msg.tool_calls[0].name == "echo"
        assert msg.tool_calls[0].arguments == {"text": "hi"}

    def test_dict_arguments_kept_as_is(self):
        d = {
            "role": "assistant",
            "tool_calls": [self._wire_tool_call({"text": "hi"})],
        }
        msg = from_openai_dict(d)
        assert msg.tool_calls is not None
        assert msg.tool_calls[0].arguments == {"text": "hi"}

    def test_unparseable_arguments_yield_parse_error_sentinel(self):
        """Tolerant fallback: never raise, preserve the raw string."""
        d = {
            "role": "assistant",
            "tool_calls": [self._wire_tool_call("not json at all")],
        }
        msg = from_openai_dict(d)
        assert msg.tool_calls is not None
        assert msg.tool_calls[0].arguments == {
            "_parse_error": True,
            "raw": "not json at all",
        }

    def test_non_dict_json_arguments_yield_sentinel(self):
        d = {
            "role": "assistant",
            "tool_calls": [self._wire_tool_call("[1, 2, 3]")],
        }
        msg = from_openai_dict(d)
        assert msg.tool_calls is not None
        assert msg.tool_calls[0].arguments == {"_parse_error": True, "raw": "[1, 2, 3]"}

    def test_missing_arguments_default_to_empty_dict(self):
        d = {"role": "assistant", "tool_calls": [{"id": "c1", "function": {"name": "echo"}}]}
        msg = from_openai_dict(d)
        assert msg.tool_calls is not None
        assert msg.tool_calls[0].arguments == {}

    def test_tool_call_id_and_name_parsed(self):
        d = {"role": "tool", "content": "ok", "tool_call_id": "c1", "name": "echo"}
        msg = from_openai_dict(d)
        assert msg.tool_call_id == "c1"
        assert msg.name == "echo"

    def test_reasoning_content_not_read_from_wire(self):
        d = {"role": "assistant", "content": "hi", "reasoning_content": "internal"}
        assert from_openai_dict(d).reasoning_content is None


class TestRoundTrip:
    def test_roundtrip_assistant_with_tool_calls(self):
        original = ChatMessage(
            role="assistant",
            content="调用工具",
            tool_calls=[
                ToolCall(id="c1", name="echo", arguments={"text": "你好", "n": 1}),
                ToolCall(id="c2", name="noop", arguments={}),
            ],
        )
        assert from_openai_dict(to_openai_dict(original)) == original

    def test_roundtrip_tool_result_message(self):
        original = ChatMessage(
            role="tool",
            content='{"ok": true}',
            tool_call_id="c1",
            name="echo",
        )
        assert from_openai_dict(to_openai_dict(original)) == original

    def test_roundtrip_content_none(self):
        original = ChatMessage(
            role="assistant",
            tool_calls=[ToolCall(id="c1", name="echo", arguments={"a": 1})],
        )
        assert from_openai_dict(to_openai_dict(original)) == original

    def test_roundtrip_plain_message(self):
        for role in ("system", "user", "assistant"):
            original = ChatMessage(role=role, content="text")  # type: ignore[arg-type]
            assert from_openai_dict(to_openai_dict(original)) == original


class TestStateMessageDelegation:
    """state.Message.to_openai_dict delegates to the same canonical pair."""

    def test_source_not_in_wire_format(self):
        msg = Message(role="user", content="hi", source="reminder")
        assert "source" not in msg.to_openai_dict()

    def test_delegated_output_matches_canonical_pair(self):
        msg = Message(
            role="assistant",
            content=None,
            tool_calls=(ToolCall(id="c1", name="echo", arguments={"text": "你好"}),),
        )
        assert msg.to_openai_dict() == to_openai_dict(
            ChatMessage(
                role="assistant",
                content=None,
                tool_calls=[ToolCall(id="c1", name="echo", arguments={"text": "你好"})],
            )
        )
