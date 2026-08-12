"""Tests for stream_service serialization helpers."""

from __future__ import annotations

from courtier.agent.api.services.stream_service import (
    deserialize_messages,
    serialize_messages,
)
from courtier.agent.core.state import Message
from courtier.prompts.engine import PromptEngine

# Reminder texts rendered from the core default bundles — the single source
# of truth (the old hardcoded constants in common/behavioral_rules.py were
# removed in favour of these YAML templates).
_ENGINE = PromptEngine.from_domain_directories([], locale="zh-CN")
PRE_TURN_REMINDER = _ENGINE.render("behavioral.pre_turn_reminder")
PERIODIC_REMINDER = _ENGINE.render("behavioral.periodic_reminder")


class TestSerializeDeserializeMessages:
    def test_round_trip_preserves_basic_fields(self):
        msgs = (
            Message(role="system", content="You are an agent."),
            Message(role="user", content="Task"),
            Message(role="assistant", content="Done."),
        )
        json_str = serialize_messages(msgs)
        result = deserialize_messages(json_str)
        assert result == msgs

    def test_round_trip_preserves_reminder_source(self):
        """The source field must survive session persistence so reminders can
        be deduplicated after a multi-turn session resumes.
        """
        msgs = (
            Message(role="system", content="You are an agent."),
            Message(role="user", content="Task"),
            Message(role="user", content=PRE_TURN_REMINDER, source="reminder"),
            Message(role="user", content=PERIODIC_REMINDER, source="reminder"),
        )
        json_str = serialize_messages(msgs)
        result = deserialize_messages(json_str)
        assert result == msgs

    def test_round_trip_preserves_tool_message_name(self):
        msgs = (
            Message(
                role="tool",
                content='{"data":"x"}',
                tool_call_id="tc_1",
                name="echo",
            ),
        )
        json_str = serialize_messages(msgs)
        result = deserialize_messages(json_str)
        assert result == msgs

    def test_deserialize_empty_list(self):
        result = deserialize_messages("[]")
        assert result == ()

    def test_omitted_optional_fields_are_none(self):
        msgs = (Message(role="user", content="hi"),)
        json_str = serialize_messages(msgs)
        result = deserialize_messages(json_str)
        assert result[0].tool_call_id is None
        assert result[0].name is None
        assert result[0].source is None
