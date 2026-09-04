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


class TestMultimodalRoundTrip:
    """T6: 媒体 part 消息的持久化往返（引用形态，无字节）。"""

    def test_serialize_part_message_persists_refs(self):
        from courtier.agent.api.services.stream_service import (
            deserialize_messages,
            serialize_messages,
        )
        from courtier.agent.core.content_parts import MediaPart, TextPart
        from courtier.agent.core.state import Message

        msgs = (
            Message(
                role="user",
                content=[TextPart("看这张图"), MediaPart("image", "file_1", "a.png")],
            ),
            Message(role="assistant", content="好的"),
        )
        raw = serialize_messages(msgs)
        assert '"file_1"' in raw
        assert "base64" not in raw
        restored = deserialize_messages(raw)
        assert restored[0].content == [
            TextPart("看这张图"),
            MediaPart("image", "file_1", "a.png"),
        ]
        assert restored[1].content == "好的"

    def test_deserialize_legacy_plain_messages(self):
        import json

        from courtier.agent.api.services.stream_service import deserialize_messages

        raw = json.dumps([{"role": "user", "content": "hello"}])
        (msg,) = deserialize_messages(raw)
        assert msg.content == "hello"

    def test_edit_truncation_with_media_turn(self):
        """compute_turn_truncation 对含媒体轮次的历史可正常裁剪。"""
        from courtier.agent.api.models import SessionRecord
        from courtier.agent.api.services.session_service import compute_turn_truncation
        from courtier.agent.api.services.stream_service import (
            deserialize_messages,
            serialize_messages,
        )
        from courtier.agent.core.content_parts import MediaPart, TextPart
        from courtier.agent.core.state import Message

        msgs = (
            Message(
                role="user",
                content=[TextPart("turn-1"), MediaPart("image", "file_1", "a.png")],
            ),
            Message(role="assistant", content="r1"),
            Message(role="user", content="turn-2"),
            Message(role="assistant", content="r2"),
        )
        session = SessionRecord(
            id="sess_x",
            task="turn-1",
            file_id="",
            messages_json=serialize_messages(msgs),
            turn_messages=[
                {"text": "turn-1", "timestamp": 1.0, "attachments": []},
                {"text": "turn-2", "timestamp": 2.0, "attachments": []},
            ],
            turn_step_starts=[0, 2],
        )
        kwargs = compute_turn_truncation(session, 1)
        cut = deserialize_messages(kwargs["messages_json"])
        assert len(cut) == 2
        assert cut[0].content == [TextPart("turn-1"), MediaPart("image", "file_1", "a.png")]
