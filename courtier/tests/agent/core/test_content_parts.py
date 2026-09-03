"""Tests for the multimodal content-part contract (T1)."""

import json

import pytest

from courtier.agent.core.content_parts import (
    MEDIA_KINDS,
    MediaPart,
    TextPart,
    content_to_plain_text,
    ensure_parts_allowed,
    iter_media_parts,
    parse_content,
    parts_to_internal,
)
from courtier.agent.core.protocol import ChatMessage, from_openai_dict, to_openai_dict
from courtier.agent.core.state import Message

PNG_PART = MediaPart(kind="image", file_id="file_1", name="photo.png")
WAV_PART = MediaPart(kind="audio", file_id="file_2", name="note.wav")
TASK = "分析这张图片"


class TestParseContent:
    def test_str_passthrough(self):
        assert parse_content("hello") == "hello"

    def test_none_passthrough(self):
        assert parse_content(None) is None

    def test_non_list_garbage_becomes_str(self):
        assert parse_content(42) == "42"

    def test_list_with_typed_parts(self):
        raw = [
            {"type": "text", "text": TASK},
            {"type": "media", "kind": "image", "file_id": "file_1", "name": "photo.png"},
        ]
        assert parse_content(raw) == [TextPart(text=TASK), PNG_PART]

    def test_media_name_defaults_empty(self):
        raw = [{"type": "media", "kind": "audio", "file_id": "file_2"}]
        assert parse_content(raw) == [MediaPart(kind="audio", file_id="file_2", name="")]

    def test_unknown_kind_preserved_as_text(self):
        raw = [{"type": "media", "kind": "hologram", "file_id": "file_9"}]
        parts = parse_content(raw)
        assert parts == [TextPart(text=json.dumps(raw[0], ensure_ascii=False))]

    def test_unrecognized_dict_preserved_losslessly(self):
        raw = [{"weird": True}]
        assert parse_content(raw) == [TextPart(text='{"weird": true}')]

    def test_non_dict_items_stringified(self):
        assert parse_content([1, "ok"]) == [TextPart(text="1"), TextPart(text="ok")]

    def test_dataclass_passthrough(self):
        parts = [TextPart(TASK), PNG_PART]
        assert parse_content(parts) == parts


class TestPartsToInternal:
    def test_str_none_passthrough(self):
        assert parts_to_internal("x") == "x"
        assert parts_to_internal(None) is None

    def test_roundtrip_with_parse(self):
        content = [TextPart(TASK), PNG_PART, WAV_PART]
        assert parse_content(parts_to_internal(content)) == content


class TestPlainTextView:
    def test_plain_strings(self):
        assert content_to_plain_text(None) == ""
        assert content_to_plain_text("hi") == "hi"

    def test_media_become_markers(self):
        text = content_to_plain_text([TextPart(TASK), PNG_PART, WAV_PART])
        assert TASK in text
        assert "[附件: photo.png（图片）]" in text
        assert "[附件: note.wav（音频）]" in text

    def test_media_without_name_uses_file_id(self):
        text = content_to_plain_text([MediaPart(kind="video", file_id="f9")])
        assert "[附件: f9（视频）]" in text

    def test_unknown_kind_label_falls_back(self):
        part = MediaPart(kind="image", file_id="f", name="a.png")
        object.__setattr__(part, "kind", "?")  # bypass Literal for label fallback
        assert "[附件: a.png（?）]" in content_to_plain_text([part])


class TestIterMediaParts:
    def test_plain_content_has_no_media(self):
        assert iter_media_parts("x") == []
        assert iter_media_parts(None) == []

    def test_filters_media_only(self):
        content: list = [TextPart(TASK), PNG_PART, TextPart("再看"), WAV_PART]
        assert iter_media_parts(content) == [PNG_PART, WAV_PART]


class TestRoleContract:
    def test_user_message_may_carry_parts(self):
        msg = ChatMessage(role="user", content=[TextPart(TASK), PNG_PART])
        assert msg.content == [TextPart(TASK), PNG_PART]

    @pytest.mark.parametrize("role", ["system", "assistant", "tool"])
    def test_non_user_roles_reject_parts(self, role):
        with pytest.raises(ValueError, match="only user messages"):
            ChatMessage(role=role, content=[TextPart(TASK)])

    @pytest.mark.parametrize("role", ["system", "assistant", "tool"])
    def test_non_user_roles_reject_parts_in_state(self, role):
        with pytest.raises(ValueError):
            Message(role=role, content=[TextPart(TASK)])


class TestWireRoundTrip:
    def test_str_message_unchanged(self):
        msg = ChatMessage(role="user", content="hello")
        d = to_openai_dict(msg)
        assert d["content"] == "hello"
        assert from_openai_dict(d).content == "hello"

    def test_part_message_roundtrip(self):
        msg = ChatMessage(role="user", content=[TextPart(TASK), PNG_PART])
        d = to_openai_dict(msg)
        assert d["content"] == [
            {"type": "text", "text": TASK},
            {"type": "media", "kind": "image", "file_id": "file_1", "name": "photo.png"},
        ]
        parsed = from_openai_dict(d)
        assert parsed.role == "user"
        assert parsed.content == [TextPart(TASK), PNG_PART]

    def test_state_message_roundtrip_via_protocol(self):
        msg = Message(role="user", content=[TextPart(TASK), WAV_PART])
        d = msg.to_openai_dict()
        assert d["content"][1] == {
            "type": "media",
            "kind": "audio",
            "file_id": "file_2",
            "name": "note.wav",
        }


class TestMediaKinds:
    def test_known_kinds(self):
        assert MEDIA_KINDS == ("image", "audio", "video")

    def test_ensure_parts_allowed_direct(self):
        ensure_parts_allowed("user", [TextPart("x")])
        ensure_parts_allowed("assistant", "text")
        with pytest.raises(ValueError):
            ensure_parts_allowed("tool", [TextPart("x")])
