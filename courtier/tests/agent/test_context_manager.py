"""Tests for ContextManager — three-layer context budget control."""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from courtier.agent.core.context_manager import (
    CompactState,
    ContextManager,
    _align_tool_boundaries,
    _build_summary,
    _deduplicate_reminders,
    _find_last_real_user,
    _is_summary_message,
    _summarize_tool_message,
)
from courtier.agent.core.state import Message
from courtier.agent.core.tool_call import ToolCall
from courtier.prompts.engine import PromptEngine

# Reminder texts rendered from the core default bundles — the single source
# of truth (the old hardcoded constants in common/behavioral_rules.py were
# removed in favour of these YAML templates).
_ENGINE = PromptEngine.from_domain_directories([], locale="zh-CN")
PRE_TURN_REMINDER = _ENGINE.render("behavioral.pre_turn_reminder")
PERIODIC_REMINDER = _ENGINE.render("behavioral.periodic_reminder")


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
        max_context_tokens=2000,
        micro_compact_tokens=1,  # gate always active in tests
        compact_target_tokens=1500,
        recent_tool_results_tokens=10,  # keeps ~2 tiny tool results
        large_output_threshold=500,
    )


@pytest.mark.asyncio
class TestLayer1PersistLargeOutput:
    async def test_small_data_passes_through(self, mgr):
        data = {"key": "value"}
        result = (await mgr._cache.persist(data, "test_tool")).data
        assert result == data

    async def test_large_data_persisted_with_ref_id(self, mgr):
        data = {"text": "x" * 1000}
        result = (await mgr._cache.persist(data, "search_documents")).data

        assert isinstance(result, dict)
        assert result["__persisted_output__"] is True
        assert result["ref_id"] == "$ref:search_documents:1"
        assert "file" in result
        assert result["size_chars"] > 900
        assert "preview" in result

    async def test_identical_data_deduped_to_same_ref(self, mgr):
        """Persisting identical output from the same tool reuses the cache file."""
        data = {"text": "x" * 1000}
        r1 = (await mgr._cache.persist(data, "search_documents")).data
        r2 = (await mgr._cache.persist(data, "search_documents")).data
        r3 = (await mgr._cache.persist(data, "other_tool")).data

        assert r1["ref_id"] == "$ref:search_documents:1"
        # Dedup hit: no second file, no new ref.
        assert r2["ref_id"] == "$ref:search_documents:1"
        assert r2.get("dedup_hit") is True
        assert r1["file"] == r2["file"]
        # A different tool gets its own ref sequence.
        assert r3["ref_id"] == "$ref:other_tool:1"

    async def test_different_data_gets_new_ref(self, mgr):
        r1 = (await mgr._cache.persist({"text": "a" * 1000}, "search_documents")).data
        r2 = (await mgr._cache.persist({"text": "b" * 1000}, "search_documents")).data
        assert r1["ref_id"] == "$ref:search_documents:1"
        assert r2["ref_id"] == "$ref:search_documents:2"

    async def test_ref_map_tracks_all_persisted_outputs(self, mgr):
        data = {"text": "x" * 1000}
        r1 = (await mgr._cache.persist(data, "search_documents")).data
        r2 = (await mgr._cache.persist(data, "other_tool")).data

        assert mgr._cache.ref_map[r1["ref_id"]] == r1["file"]
        assert mgr._cache.ref_map[r2["ref_id"]] == r2["file"]

    async def test_all_tools_persisted_no_passthrough(self, mgr):
        """ALL tools are now persisted, including audit tools."""
        for tool_name in ["parse_document", "audit_format", "audit_content"]:
            data = {"text": "x" * 1000}
            result = (await mgr._cache.persist(data, tool_name)).data
            assert isinstance(result, dict)
            assert result["__persisted_output__"] is True

    async def test_none_data_passes_through(self, mgr):
        result = (await mgr._cache.persist(None, "test_tool")).data
        assert result is None

    async def test_files_tracked_in_cache(self, mgr):
        data = {"text": "x" * 1000}
        result = (await mgr._cache.persist(data, "tool_a")).data
        assert mgr._cache.ref_map[result["ref_id"]] == result["file"]

    async def test_file_on_disk_matches_data(self, mgr):

        data = {"text": "x" * 1000}
        result = (await mgr._cache.persist(data, "search_documents")).data

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
            Message(role="tool", content='{"raw_data":"old1"}', tool_call_id="t1", name="tool1"),
            Message(role="tool", content='{"raw_data":"old2"}', tool_call_id="t2", name="tool2"),
            Message(role="tool", content='{"raw_data":"new1"}', tool_call_id="t3", name="tool3"),
            Message(role="tool", content='{"raw_data":"new2"}', tool_call_id="t4", name="tool4"),
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
            Message(role="tool", content='{"raw_data":"1"}', tool_call_id="t1", name="tool1"),
            Message(role="tool", content='{"raw_data":"2"}', tool_call_id="t2", name="tool2"),
            Message(role="tool", content='{"raw_data":"3"}', tool_call_id="t3", name="tool3"),
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
        msgs = (
            Message(role="system", content="Sys"),
            Message(role="user", content="整理这份文件"),
            Message(role="assistant", content="好的，开始处理"),
        )
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

    @pytest.mark.asyncio
    async def test_on_compact_start_called_before_llm_when_over_budget(self, mgr, mock_model):
        """超预算时 on_compact_start 必须触发，且先于 LLM 总结调用。"""
        msgs = tuple(Message(role="user", content="x" * 3000) for _ in range(3))
        calls = 0

        async def _on_start() -> None:
            nonlocal calls
            calls += 1
            # 回调应在缓慢的 LLM 总结之前触发（此刻 generate 尚未调用）
            mock_model.generate.assert_not_called()

        result = await mgr.compact_if_needed(msgs, on_compact_start=_on_start)
        assert calls == 1
        assert len(result) < len(msgs)

    @pytest.mark.asyncio
    async def test_on_compact_start_not_called_under_budget(self, mgr):
        """未超预算直接返回，不得触发 on_compact_start（否则每个 think 都误报）。"""
        msgs = (Message(role="system", content="Short"),)
        called = False

        async def _on_start() -> None:
            nonlocal called
            called = True

        result = await mgr.compact_if_needed(msgs, on_compact_start=_on_start)
        assert result is msgs
        assert called is False


class TestEstimate:
    def test_estimate_tokens(self, mgr):
        msgs = (Message(role="user", content="hello world"),)  # 11 chars
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
        assert mgr._cache.ref_map["$ref:parse_document:1"] == ".agent_cache/parse_document_123.json"


class TestBuildSummary:
    def test_summary_includes_all_messages(self):
        msgs = (
            Message(role="system", content="Sys"),
            Message(role="user", content="Task"),
        )
        result = _build_summary(msgs)
        assert "[SYSTEM]" in result
        assert "[USER]" in result

    def test_long_assistant_content_truncated(self):
        msgs = (Message(role="assistant", content="x" * 1200),)
        result = _build_summary(msgs)
        assert "..." in result
        assert len(result) < 1200  # [ASSISTANT] prefix + 1000-char body

    def test_user_content_kept_full(self):
        """Genuine user messages carry the task — never truncate them."""
        msgs = (Message(role="user", content="x" * 1500),)
        result = _build_summary(msgs)
        assert "x" * 1500 in result

    def test_reminders_dropped(self):
        msgs = (
            Message(role="user", content=PRE_TURN_REMINDER, source="reminder"),
            Message(role="user", content="真正的任务"),
        )
        result = _build_summary(msgs)
        assert PRE_TURN_REMINDER not in result
        assert "真正的任务" in result

    def test_previous_summary_kept_full(self):
        """A previous compaction summary must enter the next merge intact."""
        summary_msg = Message(
            role="system",
            content="[上下文压缩 #1] 以下为之前对话的摘要：\n\n" + "长" * 3000,
        )
        result = _build_summary((summary_msg,))
        assert "长" * 3000 in result


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
        assert sum(1 for m in result if m.role == "user" and m.content == PRE_TURN_REMINDER) == 1
        assert sum(1 for m in result if m.role == "user" and m.content == PERIODIC_REMINDER) == 1

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
        assert sum(1 for m in result if m.content == PRE_TURN_REMINDER) == 1

    def test_user_message_matching_reminder_text_without_source_is_preserved(self):
        """A user message that coincidentally matches reminder text but lacks the
        reminder source tag must not be removed.
        """
        msgs = [
            Message(role="user", content=PRE_TURN_REMINDER),
            Message(role="user", content=PRE_TURN_REMINDER, source="reminder"),
        ]
        result = _deduplicate_reminders(msgs)
        assert any(m.content == PRE_TURN_REMINDER and m.source is None for m in result)
        assert sum(1 for m in result if m.content == PRE_TURN_REMINDER) == 2


@pytest.mark.asyncio
class TestResolveRefs:
    async def test_resolves_ref_string_to_disk_data(self, mgr):
        """A $ref:tool:N string in kwargs is replaced with the loaded JSON."""
        data = {"pages": [{"text": "x" * 1000}]}
        (await mgr._cache.persist(data, "parse_document")).data

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
        (await mgr._cache.persist(data, "audit_format")).data

        kwargs = {"items": ["$ref:audit_format:1", "plain_text"]}
        resolved = mgr.resolve_refs(kwargs)

        assert resolved["items"][0] == data
        assert resolved["items"][1] == "plain_text"

    async def test_resolves_nested_ref_in_dict(self, mgr):
        data = {"pages": "x" * 1000}
        (await mgr._cache.persist(data, "parse_document")).data

        kwargs = {"outer": {"doc": "$ref:parse_document:1"}}
        resolved = mgr.resolve_refs(kwargs)

        assert resolved["outer"]["doc"] == data

    async def test_multiple_refs_in_one_call(self, mgr):
        data1 = {"pages": "x" * 1000}
        data2 = {"violations": "x" * 1000}
        (await mgr._cache.persist(data1, "parse_document")).data
        (await mgr._cache.persist(data2, "audit_format")).data

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
        (await mgr._cache.persist(data, "search_documents")).data

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
        marker = (await mgr._cache.persist(data, "parse_document")).data

        # LLM passes the marker dict directly (because schema says type: object)
        kwargs = {"document": marker, "doc_type": "通知"}
        resolved = mgr.resolve_refs(kwargs)

        assert resolved["document"] == data
        assert resolved["doc_type"] == "通知"

    async def test_marker_dict_nested_in_list(self, mgr):
        data = {"result": "x" * 1000}
        marker = (await mgr._cache.persist(data, "audit_format")).data

        kwargs = {"items": [marker, "plain"]}
        resolved = mgr.resolve_refs(kwargs)

        assert resolved["items"][0] == data
        assert resolved["items"][1] == "plain"

    async def test_marker_dict_nested_in_dict(self, mgr):
        data = {"violations": ["x" * 1000]}
        marker = (await mgr._cache.persist(data, "audit_format")).data

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
        marker = (await mgr._cache.persist(data, "search_documents")).data

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
        marker = (await mgr._cache.persist(original_data, "parse_document")).data

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
        marker = (await mgr._cache.persist(data, "search_documents")).data
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
        result = (await mgr._cache.persist(None, "test_tool")).data
        assert result is None


class TestFindLastRealUser:
    def test_skips_reminders(self):
        real = Message(role="user", content="审核这份公文")
        msgs = [
            Message(role="system", content="Sys"),
            real,
            Message(role="assistant", content="Think"),
            Message(role="user", content=PRE_TURN_REMINDER, source="reminder"),
        ]
        assert _find_last_real_user(msgs) is real

    def test_skips_periodic_reminder_too(self):
        real = Message(role="user", content="继续")
        msgs = [
            real,
            Message(role="user", content=PERIODIC_REMINDER, source="reminder"),
        ]
        assert _find_last_real_user(msgs) is real

    def test_only_reminders_returns_none(self):
        msgs = [Message(role="user", content=PRE_TURN_REMINDER, source="reminder")]
        assert _find_last_real_user(msgs) is None

    def test_empty_content_skipped(self):
        real = Message(role="user", content="task")
        msgs = [real, Message(role="user", content="")]
        assert _find_last_real_user(msgs) is real


@pytest.mark.asyncio
class TestFullCompactSkipsReminders:
    async def test_compaction_keeps_real_task_verbatim(self, mgr, mock_model):
        """The last genuine user message is the partial-compaction boundary —
        it must survive verbatim while older history is summarized away."""
        real_task = "审核这份关于XX的通知 " * 300  # ~3600 chars
        msgs = (
            Message(role="system", content="Sys"),
            Message(role="user", content="第一轮问题 " * 400),  # push over budget
            Message(role="user", content=real_task),
            Message(role="user", content=PRE_TURN_REMINDER, source="reminder"),
        )
        result = await mgr.compact_if_needed(msgs)

        assert mock_model.generate.called  # compaction actually ran
        # The boundary task is preserved verbatim; the older turn is summarized.
        assert any(m.content == real_task for m in result)
        assert not any(m.content == "第一轮问题 " * 400 for m in result)
        # History collapsed into a single summary message after the system prompt.
        assert _is_summary_message(result[1])


class TestAlignToolBoundaries:
    @staticmethod
    def _tc(call_id: str, name: str = "tool") -> ToolCall:
        return ToolCall(id=call_id, name=name, arguments={})

    def test_leading_orphan_tool_messages_dropped(self):
        msgs = [
            Message(role="tool", content="r1", tool_call_id="t1", name="tool1"),
            Message(role="tool", content="r2", tool_call_id="t2", name="tool2"),
            Message(role="user", content="next"),
        ]
        aligned = _align_tool_boundaries(msgs)
        assert [m.role for m in aligned] == ["user"]

    def test_dangling_tool_calls_stripped(self):
        assistant = Message(
            role="assistant",
            content="",
            tool_calls=(self._tc("t1"), self._tc("t2")),
        )
        msgs = [
            assistant,
            Message(role="tool", content="r1", tool_call_id="t1", name="tool1"),
        ]
        aligned = _align_tool_boundaries(msgs)
        # t2 has no answer → stripped; t1 kept; assistant has no content but
        # still carries one valid call, so it survives.
        assert len(aligned) == 2
        assert aligned[0].tool_calls is not None
        assert [tc.id for tc in aligned[0].tool_calls] == ["t1"]

    def test_assistant_with_only_dangling_calls_and_no_content_dropped(self):
        msgs = [
            Message(role="assistant", content="", tool_calls=(self._tc("t9"),)),
            Message(role="user", content="next"),
        ]
        aligned = _align_tool_boundaries(msgs)
        assert [m.role for m in aligned] == ["user"]

    def test_assistant_with_dangling_calls_but_content_kept_as_text(self):
        msgs = [
            Message(role="assistant", content="一些推理", tool_calls=(self._tc("t9"),)),
        ]
        aligned = _align_tool_boundaries(msgs)
        assert len(aligned) == 1
        assert aligned[0].content == "一些推理"
        assert aligned[0].tool_calls == ()

    def test_aligned_sequence_unchanged(self):
        msgs = [
            Message(role="assistant", content="plan", tool_calls=(self._tc("t1"),)),
            Message(role="tool", content="r1", tool_call_id="t1", name="tool1"),
            Message(role="assistant", content="done"),
        ]
        aligned = _align_tool_boundaries(msgs)
        assert aligned == msgs


@pytest.mark.asyncio
class TestFallbackAlignsToolBoundaries:
    async def test_fallback_slice_never_starts_with_orphan_tool(self, mgr, mock_model):
        """When the summary LLM call fails, the fallback slice (last 8
        messages) can start mid tool-call sequence.  The orphan tool message
        must be dropped so the provider does not reject the request."""
        mock_model.generate.side_effect = RuntimeError("LLM unavailable")

        pairs: list[Message] = []
        for i in range(5):
            pairs.append(
                Message(
                    role="assistant",
                    content=f"plan {i}",
                    tool_calls=(ToolCall(id=f"t{i}", name=f"tool{i}", arguments={}),),
                )
            )
            pairs.append(
                Message(
                    role="tool",
                    content=f'result {i} {"x" * 200}',
                    tool_call_id=f"t{i}",
                    name=f"tool{i}",
                )
            )
        msgs = (
            Message(role="system", content="Sys"),
            Message(role="user", content="任务 " * 1500),  # push over 5000 budget
            *pairs,
            # Odd message count so the 8-message fallback slice starts at a
            # tool message (an orphan whose assistant is outside the slice).
            Message(role="user", content="补充说明"),
        )
        result = await mgr.compact_if_needed(msgs)

        assert mgr.state.has_compacted is True
        # Fallback kept the recent slice but realigned it: no orphan tool
        # message at the head, no dangling tool_calls anywhere.
        assert result[0].role != "tool"
        answered = {m.tool_call_id for m in result if m.role == "tool"}
        for m in result:
            if m.role == "assistant" and m.tool_calls:
                assert {tc.id for tc in m.tool_calls} <= answered


class TestCompactStatePersistence:
    def test_snapshot_load_roundtrip(self, mgr, mock_model, tmp_path):
        mgr.state.has_compacted = True
        mgr.state.last_summary = "之前的摘要"
        mgr.state.compact_count = 3

        snap = mgr.snapshot_state()
        fresh = ContextManager(model=mock_model, cache_dir=str(tmp_path / "other_cache"))
        fresh.load_state(snap)

        assert fresh.state.has_compacted is True
        assert fresh.state.last_summary == "之前的摘要"
        assert fresh.state.compact_count == 3

    def test_snapshot_has_version(self, mgr):
        assert mgr.snapshot_state()["version"] == 1

    def test_load_none_and_empty_keep_defaults(self, mgr):
        mgr.load_state(None)
        mgr.load_state({})
        mgr.load_state("not a dict")  # type: ignore[arg-type]
        assert mgr.state.has_compacted is False
        assert mgr.state.compact_count == 0

    def test_load_unknown_version_keeps_defaults(self, mgr):
        mgr.load_state({"version": 999, "has_compacted": True, "compact_count": 7})
        assert mgr.state.has_compacted is False
        assert mgr.state.compact_count == 0


class TestFork:
    def test_fork_shares_cache_but_not_compact_state(self, mgr):
        mgr.state.has_compacted = True
        mgr.state.compact_count = 5

        child = mgr.fork()

        assert child._cache is mgr._cache
        assert child._model is mgr._model
        assert child.state.has_compacted is False
        assert child.state.compact_count == 0

        # Child compactions do not leak into the parent.
        child.state.compact_count = 9
        assert mgr.state.compact_count == 5

    def test_fork_preserves_budget_config(self, mgr):
        child = mgr.fork()
        assert child.max_context_tokens == mgr.max_context_tokens
        assert child.micro_compact_tokens == mgr.micro_compact_tokens
        assert child.compact_target_tokens == mgr.compact_target_tokens
        assert child.recent_tool_results_tokens == mgr.recent_tool_results_tokens
        assert child.large_output_threshold == mgr.large_output_threshold


@pytest.mark.asyncio
class TestMicroCompactPlaceholderFile:
    async def test_placeholder_carries_ref_and_file(self, mgr):
        msgs = (
            Message(role="system", content="S"),
            Message(role="user", content="U"),
            Message(
                role="tool",
                content='{"raw_data":{"text":"old1"}}',
                tool_call_id="t1",
                name="tool1",
            ),
            Message(
                role="tool",
                content='{"raw_data":{"text":"old2"}}',
                tool_call_id="t2",
                name="tool2",
            ),
            Message(
                role="tool",
                content='{"raw_data":{"text":"new1"}}',
                tool_call_id="t3",
                name="tool3",
            ),
            Message(
                role="tool",
                content='{"raw_data":{"text":"new2"}}',
                tool_call_id="t4",
                name="tool4",
            ),
        )
        result = await mgr.micro_compact(msgs)

        placeholder = json.loads(result[2].content or "")
        assert placeholder["_omitted"] is True
        assert placeholder["ref_id"].startswith("$ref:tool1:")
        assert placeholder["file"].endswith(".json")
        assert os.path.exists(placeholder["file"])

    async def test_placeholder_reuses_layer1_marker_file(self, mgr):
        """A tool message already persisted by Layer 1 keeps its original
        ref_id and file in the placeholder (no second persist)."""
        marker = (await mgr._cache.persist({"pages": "x" * 600}, "parse_document")).data
        envelope = json.dumps({"success": True, "raw_data": marker})
        msgs = (
            Message(role="system", content="S"),
            Message(role="user", content="U"),
            Message(role="tool", content=envelope, tool_call_id="t1", name="parse_document"),
            Message(role="tool", content='{"raw_data":"n1"}', tool_call_id="t2", name="t2"),
            Message(role="tool", content='{"raw_data":"n2"}', tool_call_id="t3", name="t3"),
        )
        result = await mgr.micro_compact(msgs)

        placeholder = json.loads(result[2].content or "")
        assert placeholder["ref_id"] == marker["ref_id"]
        assert placeholder["file"] == marker["file"]


@pytest.mark.asyncio
class TestMicroCompactGate:
    async def test_gate_skips_when_under_budget(self, mock_model, tmp_path):
        """No budget pressure → tool results stay intact."""
        mgr = ContextManager(
            model=mock_model,
            cache_dir=str(tmp_path / "c"),
            micro_compact_tokens=100_000,
        )
        msgs = (
            Message(role="system", content="S"),
            *[
                Message(
                    role="tool",
                    content=f'{{"raw_data":"r{i}"}}',
                    tool_call_id=f"t{i}",
                    name=f"tool{i}",
                )
                for i in range(6)
            ],
        )
        result = await mgr.micro_compact(msgs)
        assert result == msgs

    async def test_force_bypasses_gate(self, mock_model, tmp_path):
        """Explicit compaction requests (plugin host service) skip the gate."""
        mgr = ContextManager(
            model=mock_model,
            cache_dir=str(tmp_path / "c"),
            micro_compact_tokens=100_000,  # gate would otherwise block everything
            recent_tool_results_tokens=1,  # keeps only the 2-message minimum
        )
        msgs = (
            Message(role="system", content="S"),
            *[
                Message(
                    role="tool",
                    content=f'{{"raw_data":"result-{i}"}}',
                    tool_call_id=f"t{i}",
                    name=f"tool{i}",
                )
                for i in range(5)
            ],
        )
        result = await mgr.micro_compact(msgs, force=True)
        omitted = [m for m in result if m.role == "tool" and "_omitted" in (m.content or "")]
        assert len(omitted) == 3  # 5 results − 2 kept minimum

    async def test_reminders_deduped_even_under_budget(self, mock_model, tmp_path):
        """Reminder dedup runs on every call, gate or no gate — otherwise the
        per-turn injected reminders accumulate linearly."""
        mgr = ContextManager(
            model=mock_model,
            cache_dir=str(tmp_path / "c"),
            micro_compact_tokens=100_000,
        )
        msgs = (
            Message(role="system", content="S"),
            Message(role="user", content=PRE_TURN_REMINDER, source="reminder"),
            Message(role="assistant", content="A"),
            Message(role="user", content=PRE_TURN_REMINDER, source="reminder"),
        )
        result = await mgr.micro_compact(msgs)
        reminders = [m for m in result if m.source == "reminder"]
        assert len(reminders) == 1


@pytest.mark.asyncio
class TestTokenBasedKeep:
    async def test_keep_respects_token_budget(self, mock_model, tmp_path):
        mgr = ContextManager(
            model=mock_model,
            cache_dir=str(tmp_path / "c"),
            micro_compact_tokens=1,
            recent_tool_results_tokens=100,  # ~400 ASCII chars
        )
        small = '{"raw_data":"s"}'  # 15 chars → ~3 tokens
        big = '{"raw_data":"' + "x" * 320 + '"}'  # ~335 chars → ~83 tokens
        msgs = (
            Message(role="system", content="S"),
            Message(role="tool", content=small, tool_call_id="t1", name="tool1"),
            Message(role="tool", content=big, tool_call_id="t2", name="tool2"),
            Message(role="tool", content=small, tool_call_id="t3", name="tool3"),
            Message(role="tool", content=big, tool_call_id="t4", name="tool4"),
        )
        result = await mgr.micro_compact(msgs)
        # t4 (83 tok) fits the 100 budget; t3 kept by the 2-message minimum;
        # t2 (83 tok) no longer fits → t1 and t2 are compacted.
        assert "_omitted" in (result[1].content or "")
        assert "_omitted" in (result[2].content or "")
        assert "_omitted" not in (result[3].content or "")
        assert "_omitted" not in (result[4].content or "")

    async def test_minimum_two_kept_even_when_single_result_exceeds_budget(
        self, mock_model, tmp_path
    ):
        mgr = ContextManager(
            model=mock_model,
            cache_dir=str(tmp_path / "c"),
            micro_compact_tokens=1,
            recent_tool_results_tokens=5,  # far below any single result
        )
        big = '{"raw_data":"' + "x" * 400 + '"}'
        msgs = (
            Message(role="system", content="S"),
            *[
                Message(role="tool", content=big, tool_call_id=f"t{i}", name=f"tool{i}")
                for i in range(4)
            ],
        )
        result = await mgr.micro_compact(msgs)
        kept = [m for m in result if m.role == "tool" and "_omitted" not in (m.content or "")]
        assert len(kept) == 2  # MIN_RECENT_TOOL_RESULTS floor


@pytest.mark.asyncio
class TestActualUsageCalibration:
    async def test_actual_usage_triggers_compaction(self, mgr, mock_model):
        """The heuristic estimate is tiny, but the provider-reported
        prompt_tokens exceeds the budget → compaction must still trigger."""
        msgs = (
            Message(role="user", content="第一个问题"),
            Message(role="assistant", content="第一个回答"),
            Message(role="user", content="当前问题"),
        )
        mgr.update_actual_usage(5000)  # budget is 2000
        await mgr.compact_if_needed(msgs)
        assert mock_model.generate.called

    async def test_actual_usage_none_is_ignored(self, mgr):
        mgr.update_actual_usage(None)
        mgr.update_actual_usage(0)
        msgs = (Message(role="user", content="hello"),)
        result = await mgr.compact_if_needed(msgs)
        assert result is msgs


@pytest.mark.asyncio
class TestPostCompactValidation:
    async def test_oversized_summary_truncated(self, mock_model, tmp_path):
        mock_model.generate = AsyncMock(
            return_value=MagicMock(content="摘要内容" * 1000, tool_calls=[])
        )
        mgr = ContextManager(
            model=mock_model,
            cache_dir=str(tmp_path / "c"),
            max_context_tokens=2000,
            compact_target_tokens=500,
        )
        msgs = tuple(Message(role="user", content="x" * 3000) for _ in range(3))
        result = await mgr.compact_if_needed(msgs)

        summary_msg = result[0]  # no system prompt in msgs
        assert "...[truncated]" in (summary_msg.content or "")
        # The stored last_summary reflects the truncated version.
        assert mgr.state.last_summary is not None
        assert "...[truncated]" in mgr.state.last_summary

    async def test_over_budget_flag_when_system_prompt_dominates(self, mock_model, tmp_path):
        mgr = ContextManager(
            model=mock_model,
            cache_dir=str(tmp_path / "c"),
            max_context_tokens=2000,
            compact_target_tokens=500,
        )
        huge_system = "系统提示 " * 2000  # ~9000 CJK chars ≈ 5850 tokens
        msgs = (
            Message(role="system", content=huge_system),
            Message(role="user", content="x" * 3000),
        )
        result = await mgr.compact_if_needed(msgs)

        assert mgr.state.last_compact_over_budget is True
        # The system prompt survives verbatim — nothing more to compress.
        assert result[0].content == huge_system

    async def test_over_budget_flag_cleared_on_successful_compaction(self, mgr):
        msgs = tuple(Message(role="user", content="x" * 3000) for _ in range(3))
        await mgr.compact_if_needed(msgs)
        # Compacted to [summary, last_user] ≈ 780 tokens < 2000 budget.
        assert mgr.state.last_compact_over_budget is False


@pytest.mark.asyncio
class TestFitTextToTokenBudget:
    async def test_fits_cjk_and_ascii(self):
        from courtier.agent.core.context_manager import _fit_text_to_token_budget

        # 100 CJK chars ≈ 65 tokens; budget 65 → no truncation
        text = "汉" * 100
        assert _fit_text_to_token_budget(text, 65) == text
        # Budget 32 → roughly half survives with the marker
        fitted = _fit_text_to_token_budget(text, 32)
        assert fitted.endswith("...[truncated]")
        assert len(fitted) < len(text)

    async def test_empty_text(self):
        from courtier.agent.core.context_manager import _fit_text_to_token_budget

        assert _fit_text_to_token_budget("", 10) == ""


@pytest.mark.asyncio
class TestPartialCompaction:
    async def test_current_turn_preserved_verbatim(self, mgr, mock_model):
        """Everything from the last genuine user message on — the current
        turn's assistant/tool exchanges — survives compaction untouched."""
        boundary_user = Message(role="user", content="当前任务")
        assistant = Message(role="assistant", content="思考中")
        tool = Message(
            role="tool",
            content='{"raw_data":"r"}',
            tool_call_id="t1",
            name="tool1",
        )
        reminder = Message(role="user", content=PRE_TURN_REMINDER, source="reminder")
        msgs = (
            Message(role="system", content="Sys"),
            Message(role="user", content="旧问题 " + "x" * 4000),
            Message(role="assistant", content="旧回答 " + "y" * 4000),
            boundary_user,
            assistant,
            tool,
            reminder,
        )
        result = await mgr.compact_if_needed(msgs)

        assert mock_model.generate.called
        tail = result[-4:]
        assert tail[0] is boundary_user
        assert tail[1] is assistant
        assert tail[2] is tool
        assert tail[3] is reminder
        # History collapsed into [system prompt, summary].
        assert result[0].content == "Sys"
        assert _is_summary_message(result[1])

    async def test_summary_message_at_head_not_treated_as_system_prompt(self, mgr, mock_model):
        """Without an original system prompt, the previous summary message
        sits at messages[0] — it must stay in the compressible history so
        the incremental merge finds it (not be preserved verbatim)."""
        msgs = tuple(Message(role="user", content="x" * 3000) for _ in range(3))
        compacted1 = await mgr.compact_if_needed(msgs)
        assert _is_summary_message(compacted1[0])  # no system prompt in msgs

        followup = (
            *compacted1,
            Message(role="assistant", content="处理中 " + "y" * 3000),
            Message(role="user", content="新问题 " + "z" * 3000),
        )
        result = await mgr.compact_if_needed(followup)

        # Exactly one summary message in the output — the old one was merged,
        # not preserved alongside the new one.
        summaries = [m for m in result if _is_summary_message(m)]
        assert len(summaries) == 1


@pytest.mark.asyncio
class TestIncrementalSummary:
    @staticmethod
    def _zh_mgr(mock_model, tmp_path):
        """Manager with the zh merge template injected (mirrors context.yaml)."""
        return ContextManager(
            model=mock_model,
            cache_dir=str(tmp_path / "c"),
            max_context_tokens=2000,
            compact_target_tokens=1500,
            compact_merge_prompt_template=(
                "你是一个上下文压缩助手。【既有摘要】\n{previous_summary}\n\n"
                "【新增对话段】\n{new_segment}\n---\n请合并并输出更新后的完整摘要。"
            ),
        )

    async def test_second_compaction_merges_previous_summary(self, mock_model, tmp_path):
        mgr = self._zh_mgr(mock_model, tmp_path)
        msgs1 = tuple(Message(role="user", content="x" * 3000) for _ in range(3))
        await mgr.compact_if_needed(msgs1)
        assert mock_model.generate.call_count == 1
        assert mgr.state.last_summary == "Compacted summary."

        followup = (
            Message(
                role="system",
                content=(
                    "[上下文压缩 #1] 以下为之前对话的摘要，请继续完成任务："
                    "\n\nCompacted summary."
                ),
            ),
            Message(role="user", content="x" * 3000),
            Message(role="assistant", content="处理中 " + "y" * 3000),
            Message(role="user", content="新问题 " + "z" * 3000),
        )
        mock_model.generate.reset_mock()
        await mgr.compact_if_needed(followup)

        assert mock_model.generate.called
        prompt_sent = mock_model.generate.call_args[0][0][0]["content"]
        assert "【既有摘要】" in prompt_sent
        assert "Compacted summary." in prompt_sent
        assert "【新增对话段】" in prompt_sent

    async def test_merge_falls_back_to_en_template(self, mock_model, tmp_path):
        """Without an injected template, the en-US protocol fallback is used."""
        mgr = ContextManager(
            model=mock_model,
            cache_dir=str(tmp_path / "c"),
            max_context_tokens=2000,
        )
        msgs1 = tuple(Message(role="user", content="x" * 3000) for _ in range(3))
        await mgr.compact_if_needed(msgs1)

        followup = (
            Message(
                role="system",
                content="[上下文压缩 #1] 摘要\n\nCompacted summary.",
            ),
            Message(role="user", content="x" * 3000),
            Message(role="assistant", content="处理中 " + "y" * 3000),
            Message(role="user", content="新问题 " + "z" * 3000),
        )
        mock_model.generate.reset_mock()
        await mgr.compact_if_needed(followup)

        prompt_sent = mock_model.generate.call_args[0][0][0]["content"]
        assert "EXISTING SUMMARY" in prompt_sent
        assert "NEW SEGMENT" in prompt_sent

    async def test_nothing_new_skips_recompaction(self, mock_model, tmp_path):
        """After compaction, if no new history accumulates before the current
        turn, re-compaction must be skipped (it would only degrade)."""
        mgr = ContextManager(
            model=mock_model,
            cache_dir=str(tmp_path / "c"),
            max_context_tokens=2000,
            compact_target_tokens=1500,
        )
        big_system = "系统 " * 3000  # dominates the budget by itself
        msgs = (
            Message(role="system", content=big_system),
            *[Message(role="user", content="x" * 3000) for _ in range(3)],
        )
        compacted1 = await mgr.compact_if_needed(msgs)
        assert mock_model.generate.call_count == 1

        result = await mgr.compact_if_needed(compacted1)
        assert mock_model.generate.call_count == 1  # no second summary call
        assert result is compacted1


class TestSummarizeToolMessage:
    def test_envelope_with_ref_and_scalars(self):
        content = json.dumps(
            {
                "success": True,
                "raw_data": {
                    "__persisted_output__": True,
                    "ref_id": "$ref:parse_document:1",
                    "file": "/tmp/x.json",
                    "title": "通知",
                    "pages": 3,
                },
            }
        )
        msg = Message(role="tool", content=content, tool_call_id="t1", name="parse_document")
        digest = _summarize_tool_message(msg)
        assert "工具=parse_document" in digest
        assert "状态=成功" in digest
        assert "ref_id=$ref:parse_document:1" in digest
        assert "title='通知'" in digest

    def test_plain_text_result(self):
        msg = Message(role="tool", content="plain result", tool_call_id="t1", name="echo")
        digest = _summarize_tool_message(msg)
        assert digest.startswith("工具=echo")
        assert "plain result" in digest

    def test_failure_status(self):
        content = json.dumps({"success": False, "raw_data": None, "error": "boom"})
        msg = Message(role="tool", content=content, tool_call_id="t1", name="x")
        assert "状态=失败" in _summarize_tool_message(msg)

    def test_omitted_placeholder_shows_ref(self):
        content = json.dumps(
            {
                "success": True,
                "raw_data": {
                    "_omitted": True,
                    "tool": "search_documents",
                    "ref_id": "$ref:search_documents:2",
                },
            }
        )
        msg = Message(role="tool", content=content, tool_call_id="t1", name="search_documents")
        digest = _summarize_tool_message(msg)
        assert "ref_id=$ref:search_documents:2" in digest


class TestSplitCompactTemplate:
    def test_splits_on_separator(self):
        from courtier.agent.core.context_manager import _split_compact_template

        body, instruction = _split_compact_template("系统部分\n---\n用户指令")
        assert body == "系统部分"
        assert instruction == "用户指令"

    def test_no_separator_uses_generic_instruction(self):
        from courtier.agent.core.context_manager import _split_compact_template

        body, instruction = _split_compact_template("只有系统部分")
        assert body == "只有系统部分"
        assert instruction == "Output the summary."

    def test_only_first_separator_splits(self):
        from courtier.agent.core.context_manager import _split_compact_template

        body, instruction = _split_compact_template("A\n---\nB\n---\nC")
        assert body == "A"
        assert instruction == "B\n---\nC"


@pytest.mark.asyncio
class TestCompactPromptTemplateInjection:
    async def test_custom_template_used_for_full_compaction(self, mock_model, tmp_path):
        mgr = ContextManager(
            model=mock_model,
            cache_dir=str(tmp_path / "c"),
            max_context_tokens=2000,
            compact_prompt_template="自定义压缩提示\n{history}\n---\n请输出摘要。",
        )
        msgs = tuple(Message(role="user", content="x" * 3000) for _ in range(3))
        await mgr.compact_if_needed(msgs)

        prompt_sent = mock_model.generate.call_args[0][0][0]["content"]
        assert prompt_sent.startswith("自定义压缩提示")
        user_msg = mock_model.generate.call_args[0][0][1]["content"]
        assert user_msg == "请输出摘要。"

    async def test_fork_propagates_templates(self, mock_model, tmp_path):
        mgr = ContextManager(
            model=mock_model,
            cache_dir=str(tmp_path / "c"),
            compact_prompt_template="T1 {history}",
            compact_merge_prompt_template="T2 {previous_summary} {new_segment}",
        )
        child = mgr.fork()
        assert child._compact_prompt_template == "T1 {history}"
        assert child._compact_merge_prompt_template == "T2 {previous_summary} {new_segment}"


@pytest.mark.asyncio
class TestMicroCompactPlaceholderRefAndSuccess:
    """Placeholders must keep ref_id and the success flag for both
    persistence shapes (Layer-1 marker and ResultSummarizer path), so a
    compacted history still proves a file was parsed successfully."""

    async def test_summarizer_path_placeholder_keeps_result_id_and_success(self, mgr):
        """raw_data dropped by the summarizer: ref comes from top-level
        result_id / metadata.stored — no second persist is needed."""
        envelope = json.dumps(
            {
                "success": True,
                "actor_type": "tool",
                "actor_name": "parse_document",
                "result_id": "$ref:parse_document:1",
                "raw_data": None,
                "summary": "解析完成",
                "metadata": {
                    "stored": {"result_id": "$ref:parse_document:1", "backend": "artifact_store"}
                },
            },
            ensure_ascii=False,
        )
        # The ref must resolve in the store for the debug "file" lookup.
        await mgr._cache.persist({"pages": []}, "parse_document", force=True)
        msgs = (
            Message(role="system", content="S"),
            Message(role="user", content="U"),
            Message(role="tool", content=envelope, tool_call_id="t1", name="parse_document"),
            Message(role="tool", content='{"raw_data":"n1"}', tool_call_id="t2", name="t2"),
            Message(role="tool", content='{"raw_data":"n2"}', tool_call_id="t3", name="t3"),
        )
        result = await mgr.micro_compact(msgs)

        placeholder = json.loads(result[2].content or "")
        assert placeholder["_omitted"] is True
        assert placeholder["ref_id"] == "$ref:parse_document:1"
        assert placeholder["success"] is True

    async def test_failed_result_placeholder_keeps_success_false(self, mgr):
        envelope = json.dumps(
            {
                "success": False,
                "actor_type": "tool",
                "actor_name": "parse_document",
                "error": "解析失败",
            },
            ensure_ascii=False,
        )
        msgs = (
            Message(role="system", content="S"),
            Message(role="user", content="U"),
            Message(role="tool", content=envelope, tool_call_id="t1", name="parse_document"),
            Message(role="tool", content='{"raw_data":"n1"}', tool_call_id="t2", name="t2"),
            Message(role="tool", content='{"raw_data":"n2"}', tool_call_id="t3", name="t3"),
        )
        result = await mgr.micro_compact(msgs)

        placeholder = json.loads(result[2].content or "")
        assert placeholder["success"] is False
        assert "ref_id" not in placeholder

    async def test_persisted_message_placeholder_keeps_success(self, mgr):
        """Non-persisted raw_data path: micro-compact persists first, and the
        placeholder carries both the fresh ref and the success flag."""
        envelope = json.dumps({"success": True, "raw_data": {"text": "旧结果"}})
        msgs = (
            Message(role="system", content="S"),
            Message(role="user", content="U"),
            Message(role="tool", content=envelope, tool_call_id="t1", name="parse_document"),
            Message(role="tool", content='{"raw_data":"n1"}', tool_call_id="t2", name="t2"),
            Message(role="tool", content='{"raw_data":"n2"}', tool_call_id="t3", name="t3"),
        )
        result = await mgr.micro_compact(msgs)

        placeholder = json.loads(result[2].content or "")
        assert placeholder["_omitted"] is True
        assert placeholder["success"] is True
        assert placeholder["ref_id"].startswith("$ref:parse_document:")

    async def test_no_repeat_forced_parse_after_micro_compact(self, mgr):
        """End-to-end: a successful parse micro-compacted away must still
        count as parsed, so the parse-before-anything guard stays quiet."""
        from courtier.agent.agents.base import _has_successful_call_for

        file_path = "/tmp/报告.docx"
        observation = json.dumps(
            {
                "success": True,
                "actor_type": "tool",
                "actor_name": "parse_document",
                "result_id": "$ref:parse_document:1",
                "raw_data": None,
                "summary": "解析完成",
            },
            ensure_ascii=False,
        )
        msgs = (
            Message(role="system", content="S"),
            Message(role="user", content="审核文档"),
            Message(
                role="assistant",
                content=None,
                tool_calls=(
                    ToolCall(id="t1", name="parse_document", arguments={"file_path": file_path}),
                ),
            ),
            Message(role="tool", content=observation, tool_call_id="t1", name="parse_document"),
            Message(role="tool", content='{"raw_data":"n1"}', tool_call_id="t2", name="t2"),
            Message(role="tool", content='{"raw_data":"n2"}', tool_call_id="t3", name="t3"),
        )
        compacted = await mgr.micro_compact(msgs)
        assert "_omitted" in (compacted[3].content or "")

        assert _has_successful_call_for(compacted, "parse_document", file_path) is True
