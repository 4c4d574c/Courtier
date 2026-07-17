"""Tests for SubAgentStreamEvent."""
import pytest

from courtier.agent.agents.subagent.events import SubAgentStreamEvent


class TestSubAgentStreamEvent:
    def test_create_token_event(self):
        event = SubAgentStreamEvent(
            kind="token", subagent_name="parser", text="分析中..."
        )
        assert event.kind == "token"
        assert event.subagent_name == "parser"
        assert event.text == "分析中..."
        assert event.detail is None
        assert event.tool_name is None

    def test_create_think_event(self):
        event = SubAgentStreamEvent(
            kind="think", subagent_name="parser",
            detail="tool_calls:parse_document"
        )
        assert event.kind == "think"
        assert event.detail == "tool_calls:parse_document"

    def test_create_tool_result_event(self):
        event = SubAgentStreamEvent(
            kind="tool_result", subagent_name="parser",
            tool_name="parse_document", tool_status="ok",
            tool_duration=1.5, tool_summary="解析完成"
        )
        assert event.kind == "tool_result"
        assert event.tool_name == "parse_document"
        assert event.tool_status == "ok"
        assert event.tool_duration == 1.5
        assert event.tool_summary == "解析完成"

    def test_create_start_event(self):
        event = SubAgentStreamEvent(
            kind="start", subagent_name="parser", task="解析文档"
        )
        assert event.kind == "start"
        assert event.task == "解析文档"

    def test_create_end_event(self):
        event = SubAgentStreamEvent(
            kind="end", subagent_name="parser",
            result={"status": "completed", "content": "done"}
        )
        assert event.kind == "end"
        assert event.result == {"status": "completed", "content": "done"}

    def test_create_conclusion_event(self):
        event = SubAgentStreamEvent(
            kind="conclusion", subagent_name="parser", text="解析完成。"
        )
        assert event.kind == "conclusion"
        assert event.text == "解析完成。"

    def test_event_is_frozen(self):
        event = SubAgentStreamEvent(kind="token", subagent_name="p")
        with pytest.raises(Exception):
            event.kind = "think"

    def test_defaults_are_none(self):
        event = SubAgentStreamEvent(kind="token", subagent_name="p")
        assert event.text is None
        assert event.detail is None
        assert event.tool_name is None
        assert event.tool_status is None
        assert event.tool_duration is None
        assert event.tool_summary is None
        assert event.task is None
        assert event.result is None
        assert event.scope_id is None

    def test_scope_id_is_preserved(self):
        event = SubAgentStreamEvent(
            kind="start", subagent_name="parser", scope_id="scope-1"
        )
        assert event.scope_id == "scope-1"
