"""Tests for ContextManager — three-layer context budget control."""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from courtier.agent.core.context_manager import (
    CompactState,
    ContextManager,
    _build_summary,
    _deduplicate_reminders,
)
from courtier.agent.core.state import Message
from courtier.common.behavioral_rules import PERIODIC_REMINDER, PRE_TURN_REMINDER


@pytest.fixture
def mock_model():
    model = MagicMock()
    model.generate = AsyncMock(
        return_value=MagicMock(
            content="Compacted summary.",
            tool_calls=[],
        )
    )
    return model


@pytest.fixture
def mgr(mock_model, tmp_path):
    return ContextManager(
        model=mock_model,
        cache_dir=str(tmp_path / ".agent_cache"),
        max_context_chars=5000,
        large_output_threshold=500,
        recent_tool_results=2,
    )


@pytest.mark.asyncio
class TestLayer1PersistLargeOutput:
    async def test_small_data_passes_through(self, mgr):
        data = {"key": "value"}
        result = await mgr.persist_large_output("test_tool", data)
        assert result == data

    async def test_large_data_persisted_with_ref_id(self, mgr):
        data = {"text": "x" * 1000}
        result = await mgr.persist_large_output("search_documents", data)

        assert isinstance(result, dict)
        assert result["__persisted_output__"] is True
        assert result["ref_id"] == "$ref:search_documents:1"
        assert "file" in result
        assert result["size_chars"] > 900
        assert "preview" in result

    async def test_ref_id_increments_per_tool(self, mgr):
        data = {"text": "x" * 1000}
        r1 = await mgr.persist_large_output("search_documents", data)
        r2 = await mgr.persist_large_output("search_documents", data)
        r3 = await mgr.persist_large_output("other_tool", data)

        assert r1["ref_id"] == "$ref:search_documents:1"
        assert r2["ref_id"] == "$ref:search_documents:2"
        assert r3["ref_id"] == "$ref:other_tool:1"

    async def test_ref_map_tracks_all_persisted_outputs(self, mgr):
        data = {"text": "x" * 1000}
        r1 = await mgr.persist_large_output("search_documents", data)
        r2 = await mgr.persist_large_output("other_tool", data)

        assert mgr._cache.ref_map[r1["ref_id"]] == r1["file"]
        assert mgr._cache.ref_map[r2["ref_id"]] == r2["file"]

    async def test_all_tools_persisted_no_passthrough(self, mgr):
        """ALL tools are now persisted, including audit tools."""
        for tool_name in ["parse_document", "audit_format", "audit_content"]:
            data = {"text": "x" * 1000}
            result = await mgr.persist_large_output(tool_name, data)
            assert isinstance(result, dict)
            assert result["__persisted_output__"] is True

    async def test_none_data_passes_through(self, mgr):
        result = await mgr.persist_large_output("test_tool", None)
        assert result is None

    async def test_files_tracked_in_cache(self, mgr):
        data = {"text": "x" * 1000}
        result = await mgr.persist_large_output("tool_a", data)
        assert mgr._cache.ref_map[result["ref_id"]] == result["file"]

    async def test_file_on_disk_matches_data(self, mgr):

        data = {"text": "x" * 1000}
        result = await mgr.persist_large_output("search_documents", data)

        filepath = result["file"]
        with open(filepath, encoding="utf-8") as f:
            loaded = json.load(f)
        assert loaded == data

@pytest.mark.asyncio
class TestLayer2MicroCompact:
    async def test_few_tools_not_compacted(self, mgr):
        msgs = (
            Message(role="system", content="System"),
            Message(role="user", content="Task"),
            Message(role="tool", content="{}", tool_call_id="t1", name="tool1"),
            Message(role="tool", content="{}", tool_call_id="t2", name="tool2"),
        )
        result = await mgr.micro_compact(msgs)
        assert len(result) == len(msgs)
        # All tool messages should be intact
        for msg in result:
            if msg.role == "tool":
                assert "_omitted" not in (msg.content or "")

    async def test_old_tools_compacted(self, mgr):
        msgs = (
            Message(role="system", content="System"),
            Message(role="user", content="Task"),
            Message(
                role="tool", content='{"raw_data":"old1"}', tool_call_id="t1", name="tool1"
            ),
            Message(
                role="tool", content='{"raw_data":"old2"}', tool_call_id="t2", name="tool2"
            ),
            Message(
                role="tool", content='{"raw_data":"new1"}', tool_call_id="t3", name="tool3"
            ),
            Message(
                role="tool", content='{"raw_data":"new2"}', tool_call_id="t4", name="tool4"
            ),
        )
        result = await mgr.micro_compact(msgs)

        # First 2 tool results should be compacted (old)
        assert "_omitted" in (result[2].content or "")
        assert "_omitted" in (result[3].content or "")
        # Last 2 should be intact
        assert "new1" in (result[4].content or "")
        assert "new2" in (result[5].content or "")

    async def test_system_and_user_always_preserved(self, mgr):
        msgs = (
            Message(role="system", content="System"),
            Message(role="user", content="Task"),
            Message(
                role="tool", content='{"raw_data":"1"}', tool_call_id="t1", name="tool1"
            ),
            Message(
                role="tool", content='{"raw_data":"2"}', tool_call_id="t2", name="tool2"
            ),
            Message(
                role="tool", content='{"raw_data":"3"}', tool_call_id="t3", name="tool3"
            ),
        )
        result = await mgr.micro_compact(msgs)
        assert result[0].role == "system"
        assert result[0].content == "System"
        assert result[1].role == "user"
        assert result[1].content == "Task"


class TestLayer3FullCompact:
    @pytest.mark.asyncio
    async def test_no_compact_when_under_budget(self, mgr):
        msgs = (Message(role="system", content="Short"),)
        result = await mgr.compact_if_needed(msgs)
        assert result is msgs

    @pytest.mark.asyncio
    async def test_compact_when_over_budget(self, mgr, mock_model):
        # Create messages exceeding the 5000-char budget
        msgs = tuple(Message(role="user", content="x" * 3000) for _ in range(3))
        result = await mgr.compact_if_needed(msgs)
        assert len(result) < len(msgs)
        assert mgr.state.has_compacted is True
        assert mgr.state.compact_count == 1
        mock_model.generate.assert_called_once()

    @pytest.mark.asyncio
    async def test_force_compact_always_runs(self, mgr, mock_model):
        msgs = (Message(role="system", content="Tiny"),)
        await mgr.force_compact(msgs)
        assert mgr.state.has_compacted is True
        mock_model.generate.assert_called_once()

    @pytest.mark.asyncio
    async def test_compact_fallback_on_error(self, mgr, mock_model):
        mock_model.generate.side_effect = RuntimeError("LLM unavailable")
        msgs = tuple(Message(role="user", content="x" * 3000) for _ in range(3))
        result = await mgr.compact_if_needed(msgs)
        assert mgr.state.has_compacted is True
        # Fallback keeps recent messages (min(8, len(msgs)) = 3), same count
        assert len(result) == len(msgs)  # all 3 kept as fallback


class TestEstimate:
    def test_estimate_chars(self, mgr):
        msgs = (
            Message(role="system", content="abc"),  # 3 chars
            Message(role="user", content="hello"),  # 5 chars
        )
        assert mgr._estimate_chars(msgs) == 8

    def test_estimate_tokens(self, mgr):
        msgs = (
            Message(role="user", content="hello world"),  # 11 chars
        )
        # Rough: chars/2
        assert mgr.estimate_tokens(msgs) == 2  # 11 ASCII chars * 0.25 ≈ 2.75 → 2


class TestCompactState:
    def test_default_values(self):
        state = CompactState()
        assert state.has_compacted is False
        assert state.last_summary is None
        assert state.compact_count == 0

    def test_cache_ref_map_tracks_ids(self, mgr):
        mgr._cache.set_ref("$ref:parse_document:1", ".agent_cache/parse_document_123.json")
        assert "$ref:parse_document:1" in mgr._cache.ref_map
        assert (
            mgr._cache.ref_map["$ref:parse_document:1"]
            == ".agent_cache/parse_document_123.json"
        )


class TestBuildSummary:
    def test_summary_includes_all_messages(self):
        msgs = (
            Message(role="system", content="Sys"),
            Message(role="user", content="Task"),
        )
        result = _build_summary(msgs)
        assert "[SYSTEM]" in result
        assert "[USER]" in result

    def test_long_content_truncated(self):
        msgs = (Message(role="user", content="x" * 600),)
        result = _build_summary(msgs)
        assert "..." in result
        assert len(result) < 700


class TestDeduplicateReminders:
    def test_keeps_last_of_each_reminder_type(self):
        """Only the most recent pre-turn and periodic reminders survive."""
        msgs = [
            Message(role="system", content="System"),
            Message(role="user", content=PRE_TURN_REMINDER, source="reminder"),
            Message(role="assistant", content="Think 1"),
            Message(role="user", content=PERIODIC_REMINDER, source="reminder"),
            Message(role="assistant", content="Think 2"),
            Message(role="user", content=PRE_TURN_REMINDER, source="reminder"),
        ]
        result = _deduplicate_reminders(msgs)
        assert result.count(msgs[-1]) == 1
        assert sum(
            1 for m in result
            if m.role == "user" and m.content == PRE_TURN_REMINDER
        ) == 1
        assert sum(
            1 for m in result
            if m.role == "user" and m.content == PERIODIC_REMINDER
        ) == 1

    def test_no_reminders_returns_unchanged(self):
        msgs = [
            Message(role="system", content="System"),
            Message(role="user", content="Task"),
            Message(role="assistant", content="Hello"),
        ]
        result = _deduplicate_reminders(msgs)
        assert result == msgs

    def test_user_messages_with_other_content_preserved(self):
        """Only exact reminder content is dropped, not regular user messages."""
        msgs = [
            Message(role="user", content=PRE_TURN_REMINDER, source="reminder"),
            Message(role="user", content="Some intermediate instruction"),
            Message(role="user", content=PRE_TURN_REMINDER, source="reminder"),
        ]
        result = _deduplicate_reminders(msgs)
        assert any(m.content == "Some intermediate instruction" for m in result)
        assert sum(
            1 for m in result
            if m.content == PRE_TURN_REMINDER
        ) == 1

    def test_user_message_matching_reminder_text_without_source_is_preserved(self):
        """A user message that coincidentally matches reminder text but lacks the
        reminder source tag must not be removed.
        """
        msgs = [
            Message(role="user", content=PRE_TURN_REMINDER),
            Message(role="user", content=PRE_TURN_REMINDER, source="reminder"),
        ]
        result = _deduplicate_reminders(msgs)
        assert any(
            m.content == PRE_TURN_REMINDER and m.source is None for m in result
        )
        assert sum(
            1 for m in result
            if m.content == PRE_TURN_REMINDER
        ) == 2


@pytest.mark.asyncio
class TestResolveRefs:
    async def test_resolves_ref_string_to_disk_data(self, mgr):
        """A $ref:tool:N string in kwargs is replaced with the loaded JSON."""
        data = {"pages": [{"text": "x" * 1000}]}
        await mgr.persist_large_output("parse_document", data)

        kwargs = {"document": "$ref:parse_document:1", "doc_type": "通知"}
        resolved = mgr.resolve_refs(kwargs)

        assert resolved["document"] == data
        assert resolved["doc_type"] == "通知"

    def test_no_refs_returns_unchanged(self, mgr):
        kwargs = {"text": "hello", "count": 5}
        resolved = mgr.resolve_refs(kwargs)
        assert resolved == kwargs

    def test_unknown_ref_keeps_original_string(self, mgr):
        kwargs = {"document": "$ref:nonexistent:99"}
        resolved = mgr.resolve_refs(kwargs)
        assert resolved["document"] == "$ref:nonexistent:99"

    async def test_resolves_nested_ref_in_list(self, mgr):
        data = {"result": "x" * 1000}
        await mgr.persist_large_output("audit_format", data)

        kwargs = {"items": ["$ref:audit_format:1", "plain_text"]}
        resolved = mgr.resolve_refs(kwargs)

        assert resolved["items"][0] == data
        assert resolved["items"][1] == "plain_text"

    async def test_resolves_nested_ref_in_dict(self, mgr):
        data = {"pages": "x" * 1000}
        await mgr.persist_large_output("parse_document", data)

        kwargs = {"outer": {"doc": "$ref:parse_document:1"}}
        resolved = mgr.resolve_refs(kwargs)

        assert resolved["outer"]["doc"] == data

    async def test_multiple_refs_in_one_call(self, mgr):
        data1 = {"pages": "x" * 1000}
        data2 = {"violations": "x" * 1000}
        await mgr.persist_large_output("parse_document", data1)
        await mgr.persist_large_output("audit_format", data2)

        kwargs = {
            "document": "$ref:parse_document:1",
            "audit_result": "$ref:audit_format:1",
        }
        resolved = mgr.resolve_refs(kwargs)

        assert resolved["document"] == data1
        assert resolved["audit_result"] == data2

    def test_non_ref_dollar_sign_not_touched(self, mgr):
        kwargs = {"price": "$100", "name": "test"}
        resolved = mgr.resolve_refs(kwargs)
        assert resolved["price"] == "$100"

    def test_empty_kwargs(self, mgr):
        resolved = mgr.resolve_refs({})
        assert resolved == {}

    async def test_file_missing_falls_back_gracefully(self, mgr):
        """If disk file is deleted, ref keeps original string."""
        data = {"text": "x" * 1000}
        await mgr.persist_large_output("search_documents", data)

        ref_id = "$ref:search_documents:1"
        filepath = mgr._cache.ref_map[ref_id]
        os.remove(filepath)

        kwargs = {"data": ref_id}
        resolved = mgr.resolve_refs(kwargs)
        assert resolved["data"] == ref_id


@pytest.mark.asyncio
class TestResolveMarkerDict:
    """Test that __persisted_output__ marker dicts are also resolved."""

    async def test_resolves_marker_dict_to_disk_data(self, mgr):
        """When LLM passes the marker dict as a parameter, load full data from disk."""
        data = {"pages": [{"text": "x" * 1000}]}
        marker = await mgr.persist_large_output("parse_document", data)

        # LLM passes the marker dict directly (because schema says type: object)
        kwargs = {"document": marker, "doc_type": "通知"}
        resolved = mgr.resolve_refs(kwargs)

        assert resolved["document"] == data
        assert resolved["doc_type"] == "通知"

    async def test_marker_dict_nested_in_list(self, mgr):
        data = {"result": "x" * 1000}
        marker = await mgr.persist_large_output("audit_format", data)

        kwargs = {"items": [marker, "plain"]}
        resolved = mgr.resolve_refs(kwargs)

        assert resolved["items"][0] == data
        assert resolved["items"][1] == "plain"

    async def test_marker_dict_nested_in_dict(self, mgr):
        data = {"violations": ["x" * 1000]}
        marker = await mgr.persist_large_output("audit_format", data)

        kwargs = {"outer": {"audit": marker}}
        resolved = mgr.resolve_refs(kwargs)

        assert resolved["outer"]["audit"] == data

    def test_non_marker_dict_untouched(self, mgr):
        """A regular dict without __persisted_output__ is not touched."""
        kwargs = {"document": {"title": "hello"}, "count": 5}
        resolved = mgr.resolve_refs(kwargs)
        assert resolved["document"] == {"title": "hello"}

    async def test_marker_dict_file_missing_falls_back(self, mgr):
        data = {"text": "x" * 1000}
        marker = await mgr.persist_large_output("search_documents", data)

        import os

        os.remove(marker["file"])

        kwargs = {"data": marker}
        resolved = mgr.resolve_refs(kwargs)
        # Should fall back to ref_id string on failure
        assert resolved["data"] == marker["ref_id"]


class TestGetRefInstructions:
    def test_returns_instruction_string(self, mgr):
        instructions = mgr.get_ref_instructions()
        assert "$ref" in instructions
        assert "list_artifacts" in instructions
        assert "get_artifact" in instructions
        assert "__persisted_output__" in instructions

    def test_instruction_is_short(self, mgr):
        instructions = mgr.get_ref_instructions()
        assert len(instructions) < 1300


@pytest.mark.asyncio
class TestRefResolutionIntegration:
    @pytest.mark.asyncio
    async def test_persist_then_resolve_roundtrip(self, mgr):
        """Full flow: persist large output, then resolve ref back to original data."""
        original_data = {"pages": [{"text": "Document paragraph " * 100}]}
        marker = await mgr.persist_large_output("parse_document", original_data)

        # Marker has ref_id
        assert marker["__persisted_output__"] is True
        assert "ref_id" in marker

        # Simulate LLM passing ref_id as tool argument
        ref_id = marker["ref_id"]
        kwargs = {"document": ref_id, "doc_type": "通知"}

        # Resolve
        resolved = mgr.resolve_refs(kwargs)
        assert resolved["document"] == original_data
        assert resolved["doc_type"] == "通知"

    @pytest.mark.asyncio
    async def test_compact_preserves_ref_map(self, mgr, mock_model):
        """After full compaction, ref_map still works."""
        data = {"big": "data " * 200}
        marker = await mgr.persist_large_output("search_documents", data)
        ref_id = marker["ref_id"]

        # Create messages exceeding budget to trigger compaction
        msgs = tuple(Message(role="user", content="x" * 3000) for _ in range(3))
        await mgr.compact_if_needed(msgs)

        # ref_map should still resolve
        kwargs = {"data": ref_id}
        resolved = mgr.resolve_refs(kwargs)
        assert resolved["data"] == data


@pytest.mark.asyncio
class TestPersistWithSchema:
    async def test_none_data_does_not_create_schema(self, mgr):
        """None data should not create any schema file."""
        result = await mgr.persist_large_output("test_tool", None)
        assert result is None
