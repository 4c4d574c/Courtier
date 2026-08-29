"""ContextManager suite — rewritten per docs/architecture/context-manager-test-plan.md.

Sections (outline IDs cited in each docstring):
  §1  Token estimation & budget calibration
  §2  Layer 1 $ref (ContextManager side; store-level cases live in the
      artifacts suite)
  §3  Layer 2 micro-compact
  §4  Layer 3 full compaction + digest/alignment helpers
  §5  CompactState persistence & fork

All models are mocks; disk is tmp_path-scoped.  The legacy
tests/agent/test_context_manager.py is deleted once its coverage is fully
re-homed here (plan task 9).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from courtier.agent.core.context_manager import (
    ContextManager,
    _align_tool_boundaries,
    _build_summary,
    _deduplicate_reminders,
    _find_last_real_user,
    _fit_text_to_token_budget,
    _is_summary_message,
    _split_compact_template,
    _summarize_tool_message,
)
from courtier.agent.core.memory_manager import MemoryManager
from courtier.agent.core.state import Message

from .conftest import (
    PERIODIC_REMINDER,
    PRE_TURN_REMINDER,
    assistant,
    compact_summary,
    reminder_msg,
    system,
    tool_call,
    tool_msg,
    user,
)

# ===========================================================================
# §1 Token estimation & budget calibration
# ===========================================================================


class TestTokenEstimationAndCalibration:
    def test_estimate_empty_and_none_content(self, mgr):
        """1.1 空消息元组、content=None → 估算为 0。"""
        assert mgr.estimate_tokens(()) == 0
        assert mgr.estimate_tokens((Message(role="user", content=None),)) == 0

    def test_estimate_counts_all_cjk_blocks(self, mgr):
        """1.2 基本区/扩展A/扩展B/兼容区均按 CJK 计权（unicodedata 判定）。"""
        text = "汉㐀𠀀豈"  # U+6C49, U+3400, U+20000, U+F900
        assert mgr.estimate_tokens((Message(role="user", content=text),)) == int(
            4 * 0.65
        )

    def test_estimate_mixed_cjk_ascii_weighting(self, mgr):
        """1.3 中英混排 0.65/0.25 双权重比例。"""
        msgs = (Message(role="user", content="汉" * 10 + "a" * 40),)
        assert mgr.estimate_tokens(msgs) == int(10 * 0.65 + 40 * 0.25)

    def test_estimate_counts_tool_call_arguments(self, mgr):
        """1.4 content=None 但带 tool_calls：参数 JSON 计入估算。"""
        with_args = Message(
            role="assistant",
            content=None,
            tool_calls=(tool_call("t1", "tool", {"文件": "报告"}),),
        )
        without = Message(role="assistant", content=None)
        assert mgr.estimate_tokens((without,)) == 0
        assert mgr.estimate_tokens((with_args,)) > 0

    def test_budget_usage_takes_max_with_actual(self, mgr):
        """1.5 estimate 与 actual 恰好相等 → max 语义稳定；actual 更高则以 actual 为准。"""
        msgs = (Message(role="user", content="hello"),)
        mgr.update_actual_usage(mgr.estimate_tokens(msgs))
        assert mgr.budget_usage(msgs) == mgr.estimate_tokens(msgs)
        mgr.update_actual_usage(9999)
        assert mgr.budget_usage(msgs) == 9999

    def test_actual_usage_overwrites_not_max(self, mgr):
        """1.6 (pin) update_actual_usage 是覆盖语义：新报告覆盖旧值。

        校准值跟随"最近一次模型调用"，不是历史峰值——压缩后清空（1.8）依赖这一点。
        """
        mgr.update_actual_usage(5000)
        mgr.update_actual_usage(10)
        assert mgr.budget_usage((Message(role="user", content="hello"),)) == 10

    def test_actual_usage_ignores_none_and_zero(self, mgr):
        """1.6 补充：None/0 视为"无报告"，不覆盖也不清空。"""
        mgr.update_actual_usage(None)
        mgr.update_actual_usage(0)
        assert mgr._last_actual_prompt_tokens is None

    def test_estimate_large_text_performance_guardrail(self, mgr):
        """1.7 (极端) 1MB 级文本估算：结果正确且耗时在护栏内。"""
        big = "汉" * 1_000_000
        start = time.perf_counter()
        tokens = mgr.estimate_tokens((Message(role="user", content=big),))
        elapsed = time.perf_counter() - start
        assert tokens == 650_000
        assert elapsed < 3.0

    async def test_calibration_reset_after_full_compact(self, mgr):
        """1.8 (修复验证 F2) 全量压缩后校准值清空，budget_usage 回落为启发式。"""
        mgr.update_actual_usage(5000)
        msgs = tuple(user("x" * 3000) for _ in range(3))
        compacted = await mgr.compact_if_needed(msgs)

        assert mgr._last_actual_prompt_tokens is None
        assert mgr.budget_usage(compacted) == mgr.estimate_tokens(compacted)


# ===========================================================================
# §2 Layer 1 $ref (ContextManager side)
# ===========================================================================


class TestLayer1Refs:
    async def test_persist_degrades_gracefully_on_disk_failure(self, mgr):
        """2.3 (修复验证) 磁盘写失败（只读目录）→ 不抛出、原样返回不落盘。"""
        import stat

        cache_dir = Path(mgr._cache.cache_dir)
        cache_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)
        try:
            result = await mgr._cache.persist({"text": "x" * 1000}, "tool1")
        finally:
            cache_dir.chmod(stat.S_IRWXU)
        assert result.persisted is False
        assert result.data == {"text": "x" * 1000}
        assert mgr._cache.ref_map == {}

    async def test_preview_truncation_respects_config(self, tmp_path):
        """2.1 preview_max_chars 生效：预览头部按配置截断并带省略说明。"""
        mgr = ContextManager(
            cache_dir=str(tmp_path / "c"),
            large_output_threshold=100,
            preview_max_chars=50,
        )
        marker = (await mgr._cache.persist({"text": "y" * 900}, "tool1")).data
        head = marker["preview"].split("\n...[truncated")[0]
        assert len(head) == 50
        assert "truncated" in marker["preview"]

    async def test_threshold_zero_persists_everything(self, tmp_path):
        """2.2a large_output_threshold=0 → 任意小数据都落盘。"""
        mgr = ContextManager(cache_dir=str(tmp_path / "c"), large_output_threshold=0)
        marker = (await mgr._cache.persist({"tiny": 1}, "tool1")).data
        assert marker["__persisted_output__"] is True

    async def test_huge_threshold_passes_everything_through(self, tmp_path):
        """2.2b 极大 threshold → 数据原样直通，不产生 ref。"""
        mgr = ContextManager(cache_dir=str(tmp_path / "c"), large_output_threshold=10**12)
        result = (await mgr._cache.persist({"big": "x" * 5000}, "tool1")).data
        assert result == {"big": "x" * 5000}
        assert mgr._cache.ref_map == {}

    async def test_oversized_payload_roundtrip(self, mgr):
        """2.4 (极端) >10MB JSON 落盘 + resolve 往返一致。"""
        data = {"text": "x" * 11_000_000}
        marker = (await mgr._cache.persist(data, "big_tool")).data
        resolved = mgr.resolve_refs({"d": marker["ref_id"]})
        assert resolved["d"] == data

    async def test_lookalike_ref_strings_not_resolved(self, mgr):
        """2.5 伪装串不误解析：$$ref、空工具名、未知 ref 保持原样。"""
        kwargs = {
            "a": "$$ref:tool1:1",
            "b": "$ref:",
            "c": "$ref:nonexistent:99",
            "d": "$ref:tool1:abc",
        }
        assert mgr.resolve_refs(kwargs) == kwargs

    async def test_repeated_resolve_is_idempotent(self, mgr):
        """2.6 同 ref 重复 resolve 幂等，源文件不动。"""
        data = {"pages": [{"text": "x" * 800}]}
        marker = (await mgr._cache.persist(data, "parse_document")).data
        ref_id = marker["ref_id"]
        file = Path(mgr._cache.ref_map[ref_id])
        before = file.read_text(encoding="utf-8")

        first = mgr.resolve_refs({"d": ref_id})
        second = mgr.resolve_refs({"d": ref_id})
        assert first == second == {"d": data}
        assert file.read_text(encoding="utf-8") == before

    # -- 迁移自旧 TestResolveRefs / TestResolveMarkerDict -------------------

    async def test_ref_string_resolves_to_disk_data(self, mgr):
        """迁移：$ref 字符串参数被替换为盘上数据。"""
        data = {"pages": [{"text": "x" * 1000}]}
        (await mgr._cache.persist(data, "parse_document")).data
        resolved = mgr.resolve_refs({"document": "$ref:parse_document:1", "doc_type": "通知"})
        assert resolved["document"] == data
        assert resolved["doc_type"] == "通知"

    def test_no_refs_returns_unchanged(self, mgr):
        """迁移：无 ref 参数原样返回；空 kwargs 幂等。"""
        kwargs = {"text": "hello", "count": 5}
        assert mgr.resolve_refs(kwargs) == kwargs
        assert mgr.resolve_refs({}) == {}

    async def test_refs_resolve_in_nested_containers(self, mgr):
        """迁移：嵌套 list / dict 内的 ref 均被解析。"""
        d1 = {"result": "x" * 1000}
        d2 = {"pages": "x" * 1000}
        (await mgr._cache.persist(d1, "audit_format")).data
        (await mgr._cache.persist(d2, "parse_document")).data
        resolved = mgr.resolve_refs(
            {"items": ["$ref:audit_format:1", "plain"], "outer": {"doc": "$ref:parse_document:1"}}
        )
        assert resolved["items"] == [d1, "plain"]
        assert resolved["outer"] == {"doc": d2}

    async def test_multiple_refs_in_one_call(self, mgr):
        """迁移：一次调用解析多个不同工具的 ref。"""
        d1, d2 = {"pages": "x" * 1000}, {"violations": "x" * 1000}
        (await mgr._cache.persist(d1, "parse_document")).data
        (await mgr._cache.persist(d2, "audit_format")).data
        resolved = mgr.resolve_refs(
            {"document": "$ref:parse_document:1", "audit_result": "$ref:audit_format:1"}
        )
        assert resolved == {"document": d1, "audit_result": d2}

    def test_non_ref_dollar_sign_not_touched(self, mgr):
        """迁移：普通 $ 文本不受影响。"""
        assert mgr.resolve_refs({"price": "$100"}) == {"price": "$100"}

    async def test_file_missing_keeps_ref_string(self, mgr):
        """迁移：盘上文件被删 → ref 保持原字符串（优雅回退）。"""
        data = {"text": "x" * 1000}
        (await mgr._cache.persist(data, "search_documents")).data
        Path(mgr._cache.ref_map["$ref:search_documents:1"]).unlink()
        assert mgr.resolve_refs({"data": "$ref:search_documents:1"}) == {
            "data": "$ref:search_documents:1"
        }

    async def test_marker_dict_resolves_to_disk_data(self, mgr):
        """迁移：LLM 把 __persisted_output__ 标记字典整体当参数 → 解析为数据。"""
        data = {"pages": [{"text": "x" * 1000}]}
        marker = (await mgr._cache.persist(data, "parse_document")).data
        assert mgr.resolve_refs({"document": marker, "doc_type": "通知"}) == {
            "document": data,
            "doc_type": "通知",
        }

    async def test_marker_dict_file_missing_falls_back_to_ref_id(self, mgr):
        """迁移：标记字典文件缺失 → 回退为 ref_id 字符串。"""
        marker = (await mgr._cache.persist({"text": "x" * 1000}, "search_documents")).data
        Path(marker["file"]).unlink()
        assert mgr.resolve_refs({"data": marker}) == {"data": marker["ref_id"]}

    def test_non_marker_dict_untouched(self, mgr):
        """迁移：无标记的普通字典原样返回。"""
        assert mgr.resolve_refs({"document": {"title": "hello"}}) == {
            "document": {"title": "hello"}
        }

    def test_set_ref_tracks_mapping(self, mgr):
        """迁移：set_ref 直接登记 ref → 路径映射。"""
        mgr._cache.set_ref("$ref:parse_document:1", ".agent_cache/parse_document_123.json")
        assert mgr._cache.ref_map["$ref:parse_document:1"] == ".agent_cache/parse_document_123.json"

    def test_get_ref_instructions_content(self, mgr):
        """迁移：ref 使用说明包含关键要素且足够简短。"""
        instructions = mgr.get_ref_instructions()
        for key in ("$ref", "list_artifacts", "get_artifact", "__persisted_output__"):
            assert key in instructions
        assert len(instructions) < 1300

    async def test_persist_resolve_roundtrip_and_compact_keeps_refs(self, mgr):
        """迁移：persist → resolve 往返；全量压缩后 ref_map 仍可解析。"""
        original = {"pages": [{"text": "Document paragraph " * 100}]}
        marker = (await mgr._cache.persist(original, "parse_document")).data
        assert mgr.resolve_refs({"document": marker["ref_id"]}) == {"document": original}

        msgs = tuple(user("x" * 3000) for _ in range(3))
        await mgr.compact_if_needed(msgs)
        assert mgr.resolve_refs({"data": marker["ref_id"]}) == {"data": original}


# ===========================================================================
# §3 Layer 2 micro-compact
# ===========================================================================


class TestLayer2MicroCompact:
    async def test_empty_history_and_no_tool_messages(self, mgr):
        """3.1 零 tool 消息 / 空历史 → 原样返回（提醒去重除外，无提醒时恒等）。"""
        assert await mgr.micro_compact(()) == ()
        msgs = (system("S"), user("U"), assistant("A"))
        assert await mgr.micro_compact(msgs) == msgs

    async def test_zero_and_negative_budgets(self, mock_model, tmp_path):
        """3.2 recent=0、gate=0/负值 → 极端配置下仍守 MIN=2 下限、不崩。"""
        for recent, gate in [(0, 1), (10, 0), (10, -5)]:
            mgr = ContextManager(
                model=mock_model,
                cache_dir=str(tmp_path / f"c{recent}{gate}"),
                micro_compact_tokens=gate,
                recent_tool_results_tokens=recent,
            )
            msgs = (
                system("S"),
                *[
                    tool_msg('{"raw_data":"r"}', f"t{i}", f"tool{i}")
                    for i in range(4)
                ],
            )
            result = await mgr.micro_compact(msgs)
            kept = [m for m in result if m.role == "tool" and "_omitted" not in (m.content or "")]
            assert len(kept) == 2  # MIN_RECENT_TOOL_RESULTS floor

    async def test_recompacting_placeholder_is_noop_and_keeps_ref(self, mgr):
        """3.3 (修复验证 F1) 已压缩占位符再次被压缩必须原样保留。"""
        msgs = (
            system("S"),
            user("U"),
            tool_msg('{"raw_data":{"text":"旧结果"}}', "t1", "tool1"),
            tool_msg('{"raw_data":"n1"}', "t2", "t2"),
            tool_msg('{"raw_data":"n2"}', "t3", "t3"),
        )
        first = await mgr.micro_compact(msgs)
        placeholder = json.loads(first[2].content or "")
        assert placeholder["_omitted"] is True
        assert placeholder["ref_id"].startswith("$ref:tool1:")

        refs_before = dict(mgr._cache.ref_map)
        second = await mgr.micro_compact(first)
        assert second[2].content == first[2].content
        assert json.loads(second[2].content or "")["ref_id"].startswith("$ref:tool1:")
        assert mgr._cache.ref_map == refs_before

    async def test_non_json_result_placeholder_has_no_ref(self, mgr):
        """3.4 (pin) 非法 JSON / 纯文本 tool 结果 → 无 ref 占位符。

        已知取舍：数据无法恢复（没有可持久化的 raw_data），仅保留 note 与
        工具名；模型侧应依赖 list_artifacts 等手段兜底。
        """
        msgs = (
            system("S"),
            user("U"),
            tool_msg("plain text result", "t1", "tool1"),
            tool_msg('{"raw_data":"n1"}', "t2", "t2"),
            tool_msg('{"raw_data":"n2"}', "t3", "t3"),
        )
        result = await mgr.micro_compact(msgs)
        placeholder = json.loads(result[2].content or "")
        assert placeholder["_omitted"] is True
        assert "ref_id" not in placeholder
        assert placeholder["tool"] == "tool1"

    async def test_empty_content_kept_and_missing_name_fallback(self, mgr):
        """3.5 (pin) keep 选择是从新到旧的前缀：预算耗尽后更旧的消息一律压缩，
        空 content 也不例外（占位符无 ref，本来就没有数据）；name=None →
        占位符回退 tool_call_{id} 命名。"""
        msgs = (
            system("S"),
            user("U"),
            tool_msg("", "t0", ""),
            tool_msg('{"raw_data":"旧"}', "t1", None),
            tool_msg('{"raw_data":"n1"}', "t2", "t2"),
            tool_msg('{"raw_data":"n2"}', "t3", "t3"),
        )
        result = await mgr.micro_compact(msgs)
        empty_ph = json.loads(result[2].content or "")
        assert empty_ph["_omitted"] is True
        assert "ref_id" not in empty_ph  # nothing to persist
        placeholder = json.loads(result[3].content or "")
        assert placeholder["_omitted"] is True
        assert placeholder["tool"] == "tool_call_t1"

    async def test_same_tool_call_id_multiple_results(self, mgr):
        """3.6 同一 tool_call_id 多条结果：占位符保留各自 tool_call_id。"""
        msgs = (
            system("S"),
            user("U"),
            tool_msg('{"raw_data":"a1"}', "t1", "tool1"),
            tool_msg('{"raw_data":"a2"}', "t1", "tool1"),
            tool_msg('{"raw_data":"n1"}', "t2", "t2"),
        )
        result = await mgr.micro_compact(msgs)
        assert json.loads(result[2].content or "")["_omitted"] is True
        assert result[2].tool_call_id == "t1"
        assert "a2" in (result[3].content or "")
        assert "n1" in (result[4].content or "")

    async def test_micro_compact_keeps_message_when_persist_fails(self, make_manager):
        """3.7 (修复验证) persist 中途失败（磁盘只读）→ 保留原消息，不崩不丢数据。"""
        import stat

        mgr = make_manager()
        cache_dir = Path(mgr._cache.cache_dir)
        cache_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)
        try:
            msgs = (
                system("S"),
                user("U"),
                tool_msg('{"raw_data":"旧结果"}', "t1", "tool1"),
                tool_msg('{"raw_data":"n1"}', "t2", "t2"),
                tool_msg('{"raw_data":"n2"}', "t3", "t3"),
            )
            result = await mgr.micro_compact(msgs)
        finally:
            cache_dir.chmod(stat.S_IRWXU)
        assert result == msgs

    async def test_thousands_of_tool_results_guardrail(self, mock_model, tmp_path):
        """3.8 (极端) 数千条 tool 消息微压缩：正确 + 耗时护栏。"""
        mgr = ContextManager(
            model=mock_model,
            cache_dir=str(tmp_path / "c"),
            micro_compact_tokens=1,
            recent_tool_results_tokens=50,
        )
        msgs = (
            system("S"),
            *[
                tool_msg(f'{{"raw_data":"r{i}"}}', f"t{i}", f"tool{i}")
                for i in range(1000)
            ],
        )
        start = time.perf_counter()
        result = await mgr.micro_compact(msgs)
        elapsed = time.perf_counter() - start
        kept = [m for m in result if m.role == "tool" and "_omitted" not in (m.content or "")]
        assert 2 <= len(kept) <= 20  # bounded by the recency budget, not by history size
        assert elapsed < 10.0

    async def test_reminder_dedup_runs_even_under_budget(self, mgr):
        """3.9 迁移：门控未开时提醒去重仍执行（否则提醒线性累积）。"""
        msgs = (
            system("S"),
            reminder_msg(PRE_TURN_REMINDER),
            assistant("A"),
            reminder_msg(PRE_TURN_REMINDER),
        )
        result = await mgr.micro_compact(msgs)
        assert sum(1 for m in result if m.source == "reminder") == 1

    async def test_micro_compact_gate_and_force(self, mock_model, tmp_path):
        """迁移：预算门控未开时工具结果原样；force=True 绕过门控。"""
        msgs = (
            system("S"),
            *[tool_msg(f'{{"raw_data":"result-{i}"}}', f"t{i}", f"tool{i}") for i in range(5)],
        )
        gated = ContextManager(
            model=mock_model, cache_dir=str(tmp_path / "g"), micro_compact_tokens=100_000
        )
        assert await gated.micro_compact(msgs) == msgs

        forced = ContextManager(
            model=mock_model,
            cache_dir=str(tmp_path / "f"),
            micro_compact_tokens=100_000,
            recent_tool_results_tokens=1,
        )
        result = await forced.micro_compact(msgs, force=True)
        omitted = [m for m in result if m.role == "tool" and "_omitted" in (m.content or "")]
        assert len(omitted) == 3  # 5 − MIN 2

    async def test_old_tools_compacted_system_user_preserved(self, mgr):
        """迁移：最旧的 tool 结果被压缩，最近两条保留；system/user 恒不动。"""
        msgs = (
            system("S"),
            user("Task"),
            tool_msg('{"raw_data":"old1"}', "t1", "tool1"),
            tool_msg('{"raw_data":"old2"}', "t2", "tool2"),
            tool_msg('{"raw_data":"new1"}', "t3", "tool3"),
            tool_msg('{"raw_data":"new2"}', "t4", "tool4"),
        )
        result = await mgr.micro_compact(msgs)
        assert "_omitted" in (result[2].content or "")
        assert "_omitted" in (result[3].content or "")
        assert "new1" in (result[4].content or "")
        assert "new2" in (result[5].content or "")
        assert result[0].content == "S"
        assert result[1].content == "Task"

    async def test_token_budget_based_keep(self, mock_model, tmp_path):
        """迁移：保留按 token 预算从新到旧选择；单条超预算仍保底 MIN=2。"""
        mgr = ContextManager(
            model=mock_model,
            cache_dir=str(tmp_path / "c"),
            micro_compact_tokens=1,
            recent_tool_results_tokens=100,
        )
        small = '{"raw_data":"s"}'
        big = '{"raw_data":"' + "x" * 320 + '"}'
        msgs = (
            system("S"),
            tool_msg(small, "t1", "tool1"),
            tool_msg(big, "t2", "tool2"),
            tool_msg(small, "t3", "tool3"),
            tool_msg(big, "t4", "tool4"),
        )
        result = await mgr.micro_compact(msgs)
        assert "_omitted" in (result[1].content or "")
        assert "_omitted" in (result[2].content or "")
        assert "_omitted" not in (result[3].content or "")
        assert "_omitted" not in (result[4].content or "")

    async def test_placeholder_reuses_layer1_marker(self, mgr):
        """迁移：Layer 1 已落盘的结果复用原 ref_id/file，不二次落盘。"""
        marker = (await mgr._cache.persist({"pages": "x" * 600}, "parse_document")).data
        msgs = (
            system("S"),
            user("U"),
            tool_msg(json.dumps({"success": True, "raw_data": marker}), "t1", "parse_document"),
            tool_msg('{"raw_data":"n1"}', "t2", "t2"),
            tool_msg('{"raw_data":"n2"}', "t3", "t3"),
        )
        result = await mgr.micro_compact(msgs)
        placeholder = json.loads(result[2].content or "")
        assert placeholder["ref_id"] == marker["ref_id"]
        assert placeholder["file"] == marker["file"]

    async def test_placeholder_keeps_ref_and_success_three_shapes(self, mgr):
        """迁移：三种持久化形态的占位符都保留 ref_id 与 success 标志。"""
        # (a) summarizer 形态：顶层 result_id + metadata.stored
        await mgr._cache.persist({"pages": []}, "parse_document", force=True)
        envelope_a = json.dumps(
            {
                "success": True,
                "actor_name": "parse_document",
                "result_id": "$ref:parse_document:1",
                "raw_data": None,
                "metadata": {"stored": {"result_id": "$ref:parse_document:1"}},
            },
            ensure_ascii=False,
        )
        # (b) 失败结果：无 ref，但 success=False 必须保留
        envelope_b = json.dumps({"success": False, "error": "解析失败"}, ensure_ascii=False)
        # (c) 未持久化 raw_data：微压缩先落盘，占位符带新 ref + success
        envelope_c = json.dumps({"success": True, "raw_data": {"text": "旧结果"}})
        msgs = (
            system("S"),
            user("U"),
            tool_msg(envelope_a, "t1", "parse_document"),
            tool_msg(envelope_b, "t2", "parse_document"),
            tool_msg(envelope_c, "t3", "parse_document"),
            tool_msg('{"raw_data":"n1"}', "t4", "t4"),
            tool_msg('{"raw_data":"n2"}', "t5", "t5"),
        )
        result = await mgr.micro_compact(msgs)
        pa = json.loads(result[2].content or "")
        pb = json.loads(result[3].content or "")
        pc = json.loads(result[4].content or "")
        assert pa["ref_id"] == "$ref:parse_document:1" and pa["success"] is True
        assert pb["success"] is False and "ref_id" not in pb
        assert pc["ref_id"].startswith("$ref:parse_document:") and pc["success"] is True

    async def test_fresh_persist_placeholder_carries_file_on_disk(self, mgr):
        """迁移补充：fresh-persist 占位符带 file 路径且文件真实存在。"""
        from pathlib import Path

        msgs = (
            system("S"),
            user("U"),
            tool_msg('{"raw_data":{"text":"旧结果"}}', "t1", "tool1"),
            tool_msg('{"raw_data":"n1"}', "t2", "t2"),
            tool_msg('{"raw_data":"n2"}', "t3", "t3"),
        )
        result = await mgr.micro_compact(msgs)
        placeholder = json.loads(result[2].content or "")
        assert placeholder["file"].endswith(".json")
        assert Path(placeholder["file"]).exists()

    async def test_compacted_history_still_proves_successful_parse(self, mgr):
        """迁移（端到端）：成功的 parse 被微压缩后 parse 守卫不再要求重解析。"""
        from courtier.agent.agents.base import _has_successful_call_for
        file_path = "/tmp/报告.docx"
        observation = json.dumps(
            {
                "success": True,
                "actor_name": "parse_document",
                "result_id": "$ref:parse_document:1",
                "raw_data": None,
            },
            ensure_ascii=False,
        )
        msgs = (
            system("S"),
            user("审核文档"),
            assistant(tool_calls=(tool_call("t1", "parse_document", {"file_path": file_path}),)),
            tool_msg(observation, "t1", "parse_document"),
            tool_msg('{"raw_data":"n1"}', "t2", "t2"),
            tool_msg('{"raw_data":"n2"}', "t3", "t3"),
        )
        compacted = await mgr.micro_compact(msgs)
        assert "_omitted" in (compacted[3].content or "")
        assert _has_successful_call_for(compacted, "parse_document", file_path) is True


class TestReminderDeduplication:
    """3.9 子组：提醒去重的单元行为（迁移自旧 TestDeduplicateReminders）。"""

    def test_keeps_last_of_each_reminder_type(self):
        msgs = [
            system("System"),
            reminder_msg(PRE_TURN_REMINDER),
            assistant("Think 1"),
            reminder_msg(PERIODIC_REMINDER),
            assistant("Think 2"),
            reminder_msg(PRE_TURN_REMINDER),
        ]
        result = _deduplicate_reminders(msgs)
        assert (
            sum(1 for m in result if m.content == PRE_TURN_REMINDER and m.source == "reminder") == 1
        )
        assert sum(1 for m in result if m.content == PERIODIC_REMINDER) == 1

    def test_no_reminders_returns_same_content(self):
        msgs = [system("System"), user("Task"), assistant("Hello")]
        assert _deduplicate_reminders(msgs) == msgs

    def test_user_messages_with_other_content_preserved(self):
        msgs = [
            reminder_msg(PRE_TURN_REMINDER),
            user("Some intermediate instruction"),
            reminder_msg(PRE_TURN_REMINDER),
        ]
        result = _deduplicate_reminders(msgs)
        assert any(m.content == "Some intermediate instruction" for m in result)
        assert sum(1 for m in result if m.content == PRE_TURN_REMINDER) == 1

    def test_matching_text_without_source_tag_is_preserved(self):
        """同文但无 reminder source 的真实用户消息不被误删。"""
        msgs = [
            Message(role="user", content=PRE_TURN_REMINDER),
            reminder_msg(PRE_TURN_REMINDER),
        ]
        result = _deduplicate_reminders(msgs)
        assert sum(1 for m in result if m.content == PRE_TURN_REMINDER) == 2


# ===========================================================================
# §4 Layer 3 full compaction
# ===========================================================================


class TestLayer3FullCompaction:
    async def test_under_budget_returns_identical_tuple(self, mgr):
        """迁移：未超预算原样返回（同一元组对象，不触发回调）。"""
        msgs = (system("Short"),)
        called = False

        async def _on_start() -> None:
            nonlocal called
            called = True

        result = await mgr.compact_if_needed(msgs, on_compact_start=_on_start)
        assert result is msgs
        assert called is False

    async def test_on_compact_start_fires_before_llm_when_over_budget(
        self, mgr, mock_model
    ):
        """迁移：超预算时 on_compact_start 恰好触发一次，且先于 LLM 总结。"""
        msgs = tuple(user("x" * 3000) for _ in range(3))
        calls = 0

        async def _on_start() -> None:
            nonlocal calls
            calls += 1
            mock_model.generate.assert_not_called()

        result = await mgr.compact_if_needed(msgs, on_compact_start=_on_start)
        assert calls == 1
        assert len(result) < len(msgs)

    async def test_over_budget_compacts_once(self, mgr, mock_model):
        """迁移：超预算触发压缩，状态与编号正确。"""
        msgs = tuple(user("x" * 3000) for _ in range(3))
        result = await mgr.compact_if_needed(msgs)
        assert len(result) < len(msgs)
        assert mgr.state.has_compacted is True
        assert mgr.state.compact_count == 1
        assert mock_model.generate.call_count == 1

    async def test_force_compact_runs_regardless_of_budget(self, mgr, mock_model):
        """迁移：force_compact 不看预算，连当前轮一起压缩。"""
        msgs = (system("Sys"), user("整理这份文件"), assistant("好的，开始处理"))
        await mgr.force_compact(msgs)
        assert mgr.state.has_compacted is True
        mock_model.generate.assert_called_once()

    async def test_boundary_fallbacks_do_not_crash(self, make_manager, mock_model):
        """4.1 (边界) 空元组 / 仅 system / 仅 assistant / 仅 reminder 的回退路径。"""
        m = make_manager()
        assert await m.compact_if_needed(()) == ()

        # force_compact on empty: boundary=0, nothing compressible → flagged.
        m = make_manager()
        assert await m.force_compact(()) == ()
        assert m.state.last_compact_over_budget is True

        # Only an oversized system prompt: nothing compressible → flagged.
        m = make_manager()
        huge_system = system("系统提示 " * 2000)
        result = await m.compact_if_needed((huge_system,))
        assert result == (huge_system,)
        assert m.state.last_compact_over_budget is True

        # Only assistant content: boundary falls to the end, all summarized.
        m = make_manager()
        result = await m.compact_if_needed((system("S"), assistant("y" * 9000)))
        assert mock_model.generate.called
        assert len(result) == 2 and _is_summary_message(result[1])

        # Only reminders: reminders are dropped from the digest, still compacts.
        m = make_manager()
        result = await m.compact_if_needed((system("S"), reminder_msg("r" * 9000)))
        assert m.state.has_compacted is True

    async def test_model_none_returns_messages_and_records_failed(self, mgr, monkeypatch):
        """4.2 (边界) model=None（plugin host 场景）→ 原样返回 + failed 指标。"""
        calls: list[str] = []
        monkeypatch.setattr(
            "courtier.agent.core.context_manager.record_context_compaction",
            lambda kind: calls.append(kind),
        )
        mgr._model = None
        msgs = tuple(user("x" * 3000) for _ in range(3))
        result = await mgr.compact_if_needed(msgs)
        assert result is msgs
        assert mgr.state.has_compacted is False
        assert calls == ["failed"]

    async def test_empty_model_content_falls_back_to_system_body(self, make_manager, mock_model):
        """4.3 (边界) 模型返回空 content → system_body[:500] 兜底。"""
        from unittest.mock import AsyncMock, MagicMock

        mock_model.generate = AsyncMock(return_value=MagicMock(content="", tool_calls=[]))
        mgr = make_manager(compact_prompt_template="MARKER-START {history}\n---\n请输出摘要。")
        msgs = tuple(user("x" * 3000) for _ in range(3))
        result = await mgr.compact_if_needed(msgs)
        summary = result[0].content or ""
        assert summary.startswith("[上下文压缩 #1]")
        assert "MARKER-START" in summary

    async def test_inverted_target_above_max_does_not_crash(self, make_manager, mock_model):
        """4.4 (边界) compact_target_tokens > max_context_tokens 配置倒挂可走通。"""
        mgr = make_manager(max_context_tokens=2000, compact_target_tokens=3000)
        msgs = tuple(user("x" * 3000) for _ in range(3))
        await mgr.compact_if_needed(msgs)
        assert mgr.state.has_compacted is True
        assert mgr.state.last_compact_over_budget is False

    async def test_tiny_budget_compacts_without_loop(self, make_manager, mock_model):
        """4.5 (极端) max_context_tokens=1 → 每轮必压但不死循环。"""
        mgr = make_manager(max_context_tokens=1, compact_target_tokens=1)
        msgs = (user("x" * 3000), user("第二个问题 " + "y" * 3000))
        first = await mgr.compact_if_needed(msgs)
        assert mock_model.generate.call_count == 1

        # Subsequent checks: history is all-summary → skip guard holds.
        await mgr.compact_if_needed(first)
        await mgr.compact_if_needed(first)
        assert mock_model.generate.call_count == 1

    async def test_three_successive_compactions_number_and_merge(self, mgr, mock_model):
        """4.6 连续三次压缩：编号 #1→#3，增量合并链不重压全量。"""
        msgs = tuple(user("x" * 3000) for _ in range(3))
        r1 = await mgr.compact_if_needed(msgs)

        followup = (*r1, assistant("干活 " + "y" * 3000), user("继续问题 " + "z" * 3000))
        r2 = await mgr.compact_if_needed(followup)

        followup2 = (*r2, assistant("再干活 " + "y" * 3000), user("再继续 " + "z" * 3000))
        r3 = await mgr.compact_if_needed(followup2)

        assert mgr.state.compact_count == 3
        assert mock_model.generate.call_count == 3
        assert (r3[0].content or "").startswith("[上下文压缩 #3]")
        summaries = [m for m in r3 if _is_summary_message(m)]
        assert len(summaries) == 1

    async def test_merge_segment_of_only_reminders(self, mgr, mock_model):
        """4.7 (边界) merge 段仅含 reminder → 空摘要段仍走通。"""
        msgs = tuple(user("x" * 3000) for _ in range(3))
        r1 = await mgr.compact_if_needed(msgs)
        mock_model.generate.reset_mock()

        # The reminder sits before the new boundary user → it lands in the
        # merge segment alone (digest drops reminders → empty segment).
        followup = (*r1, reminder_msg(PRE_TURN_REMINDER), user("新问题 " + "x" * 9000))
        r2 = await mgr.compact_if_needed(followup)
        assert mock_model.generate.call_count == 1
        assert mgr.state.compact_count == 2
        assert len([m for m in r2 if _is_summary_message(m)]) == 1

    async def test_on_compact_start_exception_propagates(self, mgr):
        """4.8 (极端·pin) on_compact_start 抛异常向上传播（run 失败语义）。"""
        msgs = tuple(user("x" * 3000) for _ in range(3))

        async def _boom() -> None:
            raise RuntimeError("callback exploded")

        with pytest.raises(RuntimeError, match="callback exploded"):
            await mgr.compact_if_needed(msgs, on_compact_start=_boom)

    async def test_model_failure_on_second_compaction_falls_back(self, mgr, mock_model):
        """4.9 (边界) 第二次压缩时模型才失败 → merge 路径 fallback。"""
        msgs = tuple(user("x" * 3000) for _ in range(3))
        r1 = await mgr.compact_if_needed(msgs)
        mock_model.generate.side_effect = RuntimeError("LLM unavailable")

        followup = (*r1, assistant("干活 " + "y" * 3000), user("新问题 " + "z" * 3000))
        result = await mgr.compact_if_needed(followup)
        assert "压缩失败" in (mgr.state.last_summary or "")
        assert result[0].role != "tool"
        answered = {m.tool_call_id for m in result if m.role == "tool"}
        for m in result:
            if m.role == "assistant" and m.tool_calls:
                assert {tc.id for tc in m.tool_calls} <= answered

    async def test_no_merge_compaction_right_after_full_compact(self, mgr, mock_model):
        """4.10 (修复验证 F2) 压缩后新用户轮 + 小 estimate 不触发合并压缩。"""
        mgr.update_actual_usage(5000)  # pre-compaction provider report goes stale below
        history = tuple(user("x" * 3000) for _ in range(3))
        compacted = await mgr.compact_if_needed(history)
        assert mgr.state.has_compacted
        mock_model.generate.reset_mock()

        followup = (*compacted, assistant("处理完成"), user("新问题"))
        result = await mgr.compact_if_needed(followup)
        mock_model.generate.assert_not_called()
        assert result is followup

        mgr.update_actual_usage(5000)
        await mgr.compact_if_needed(followup)
        mock_model.generate.assert_called()

    async def test_forged_summary_prefix_treated_as_history(self, mgr):
        """4.11 (pin+记录) "[上下文压缩 #"开头的 system 文本被误判为摘要。

        已知局限：system 提示由 host 构造、不受用户输入控制，影响面可控；
        行为固化：伪造消息不作为系统提示保留，而是进入可压缩历史。
        """
        forged = system("[上下文压缩 #999] 这不是真的摘要")
        msgs = (forged, user("任务 " + "x" * 9000))
        result = await mgr.compact_if_needed(msgs)
        assert not any(m is forged for m in result)
        assert len(result) == 2
        assert _is_summary_message(result[0])

    async def test_current_turn_still_over_budget_micro_fallback(self, mgr, mock_model):
        """4.12 (回归) 压缩后当前轮仍超预算 → force 微压缩兜底。"""
        msgs = (
            system("S"),
            user("旧问题 " + "x" * 4000),
            user("当前任务"),
            tool_msg('{"raw_data":"' + "y" * 9000 + '"}', "t1", "tool1"),
            tool_msg('{"raw_data":"' + "y" * 9000 + '"}', "t2", "tool2"),
            tool_msg('{"raw_data":"' + "y" * 9000 + '"}', "t3", "tool3"),
        )
        result = await mgr.compact_if_needed(msgs)

        contents = [json.loads(m.content or "{}") for m in result if m.role == "tool"]
        omitted = [c for c in contents if c.get("_omitted")]
        assert omitted, "oldest current-turn tool result must be micro-compacted"
        assert all(c.get("ref_id") for c in omitted)
        assert any(m.content == "当前任务" for m in result)

    # -- 迁移自旧 TestPartialCompaction / TestIncrementalSummary 等 ----------

    async def test_current_turn_preserved_verbatim(self, mgr):
        """迁移：最后一轮真实用户消息起的内容逐字保留。"""
        boundary_user = user("当前任务")
        a = assistant("思考中")
        t = tool_msg('{"raw_data":"r"}', "t1", "tool1")
        rem = reminder_msg(PRE_TURN_REMINDER)
        msgs = (
            system("Sys"),
            user("旧问题 " + "x" * 4000),
            assistant("旧回答 " + "y" * 4000),
            boundary_user,
            a,
            t,
            rem,
        )
        result = await mgr.compact_if_needed(msgs)
        assert result[-4] is boundary_user
        assert result[-3] is a
        assert result[-2] is t
        assert result[-1] is rem
        assert result[0].content == "Sys"
        assert _is_summary_message(result[1])

    async def test_real_task_survives_and_old_history_summarized(self, mgr):
        """迁移：真实任务消息是边界，逐字保留；旧轮被摘要。"""
        real_task = "审核这份关于XX的通知 " * 300
        msgs = (
            system("Sys"),
            user("第一轮问题 " * 400),
            user(real_task),
            reminder_msg(PRE_TURN_REMINDER),
        )
        result = await mgr.compact_if_needed(msgs)
        assert any(m.content == real_task for m in result)
        assert not any(m.content == "第一轮问题 " * 400 for m in result)
        assert _is_summary_message(result[1])

    async def test_summary_at_head_not_treated_as_system_prompt(self, mgr):
        """迁移：无原系统提示时，旧摘要留在可压缩历史以参与增量合并。"""
        msgs = tuple(user("x" * 3000) for _ in range(3))
        compacted1 = await mgr.compact_if_needed(msgs)
        assert _is_summary_message(compacted1[0])

        followup = (
            *compacted1,
            assistant("处理中 " + "y" * 3000),
            user("新问题 " + "z" * 3000),
        )
        result = await mgr.compact_if_needed(followup)
        assert len([m for m in result if _is_summary_message(m)]) == 1

    async def test_incremental_merge_uses_injected_template(self, mgr, mock_model):
        """迁移：注入 zh 合并模板时，第二次压缩走 {previous_summary}/{new_segment}。"""
        mgr2 = ContextManager(
            model=mock_model,
            max_context_tokens=2000,
            compact_target_tokens=1500,
            compact_merge_prompt_template=(
                "【既有摘要】\n{previous_summary}\n\n【新增对话段】\n{new_segment}\n---\n请合并。"
            ),
        )
        await mgr2.compact_if_needed(tuple(user("x" * 3000) for _ in range(3)))
        followup = (
            compact_summary(1),
            user("x" * 3000),
            assistant("处理中 " + "y" * 3000),
            user("新问题 " + "z" * 3000),
        )
        await mgr2.compact_if_needed(followup)
        prompt_sent = mock_model.generate.call_args[0][0][0]["content"]
        assert "【既有摘要】" in prompt_sent and "Compacted summary." in prompt_sent
        assert "【新增对话段】" in prompt_sent

    async def test_incremental_merge_falls_back_to_en_template(self, mgr, mock_model):
        """迁移：未注入模板时使用 en 协议回退。"""
        await mgr.compact_if_needed(tuple(user("x" * 3000) for _ in range(3)))
        followup = (
            compact_summary(1),
            user("x" * 3000),
            assistant("处理中 " + "y" * 3000),
            user("新问题 " + "z" * 3000),
        )
        await mgr.compact_if_needed(followup)
        prompt_sent = mock_model.generate.call_args[0][0][0]["content"]
        assert "EXISTING SUMMARY" in prompt_sent and "NEW SEGMENT" in prompt_sent

    async def test_nothing_new_skips_recompaction(self, make_manager, mock_model):
        """迁移：已压缩且历史全是摘要 → 跳过重压缩（避免摘要退化）。"""
        mgr = make_manager()
        big_system = system("系统 " * 3000)
        msgs = (big_system, *tuple(user("x" * 3000) for _ in range(3)))
        compacted1 = await mgr.compact_if_needed(msgs)
        assert mock_model.generate.call_count == 1

        result = await mgr.compact_if_needed(compacted1)
        assert mock_model.generate.call_count == 1
        assert result is compacted1

    async def test_oversized_summary_truncated_to_target(self, make_manager, mock_model):
        """迁移：摘要超出 target → 收缩到预算内，last_summary 同步截断版。"""
        from unittest.mock import AsyncMock, MagicMock

        model = mock_model
        model.generate = AsyncMock(return_value=MagicMock(content="摘要内容" * 1000, tool_calls=[]))
        mgr = ContextManager(
            model=model,
            max_context_tokens=2000,
            compact_target_tokens=500,
        )
        result = await mgr.compact_if_needed(tuple(user("x" * 3000) for _ in range(3)))
        assert "...[truncated]" in (result[0].content or "")
        assert "...[truncated]" in (mgr.state.last_summary or "")

    async def test_over_budget_flag_when_system_prompt_dominates(self, make_manager, mock_model):
        """迁移：压缩后仍超预算（system 主导）→ flag 置位，system 原样保留。"""
        mgr = make_manager(max_context_tokens=2000, compact_target_tokens=500)
        huge_system = system("系统提示 " * 2000)
        result = await mgr.compact_if_needed((huge_system, user("x" * 3000)))
        assert mgr.state.last_compact_over_budget is True
        assert result[0].content == huge_system.content

    async def test_over_budget_flag_cleared_on_success(self, mgr):
        """迁移：成功压缩回预算内 → flag 复位。"""
        await mgr.compact_if_needed(tuple(user("x" * 3000) for _ in range(3)))
        assert mgr.state.last_compact_over_budget is False

    async def test_custom_compact_template_used(self, make_manager, mock_model):
        """迁移：注入的自定义压缩模板被使用（含 --- 分割）。"""
        mgr = make_manager(compact_prompt_template="自定义压缩提示\n{history}\n---\n请输出摘要。")
        await mgr.compact_if_needed(tuple(user("x" * 3000) for _ in range(3)))
        assert mock_model.generate.call_args[0][0][0]["content"].startswith("自定义压缩提示")
        assert mock_model.generate.call_args[0][0][1]["content"] == "请输出摘要。"

    async def test_fallback_slice_keeps_recent_and_truncates_huge(self, mgr, mock_model):
        """迁移：压缩失败 → 保留最近 8 条、对齐工具边界、超长内容截断。"""
        mock_model.generate.side_effect = RuntimeError("LLM unavailable")
        msgs = (
            system("Sys"),
            *[
                Message(role="user", content=f"turn {i} " + "x" * 100)
                for i in range(12)
            ],
            user("长" * 10_000),  # in the last-8 slice → truncated to 4000 chars
        )
        result = await mgr.compact_if_needed(msgs)
        assert mgr.state.has_compacted is True
        assert len(result) <= 8
        assert result[0].role != "tool"
        assert any((m.content or "").endswith("...[truncated]") for m in result)
        assert all(len(m.content or "") <= 4100 for m in result)


# -- §4 助手函数子组（迁移自旧单元测试类） -----------------------------------


class TestBuildSummary:
    def test_roles_labelled_and_reminders_dropped(self):
        """迁移：各角色带标签；提醒不进入摘要。"""
        msgs = (
            system("Sys"),
            user("Task"),
            reminder_msg(PRE_TURN_REMINDER),
        )
        result = _build_summary(msgs)
        assert "[SYSTEM] Sys" in result
        assert "[USER] Task" in result
        assert PRE_TURN_REMINDER not in result

    def test_long_assistant_truncated_with_tool_names(self):
        """迁移：assistant 截断至 1000 字符并附工具调用名单。"""
        msgs = (
            Message(
                role="assistant",
                content="x" * 1200,
                tool_calls=(tool_call("t1", "alpha"), tool_call("t2", "beta")),
            ),
        )
        result = _build_summary(msgs)
        assert len(result) < 1200
        assert "alpha" in result and "beta" in result

    def test_user_content_kept_full(self):
        """迁移：真实用户消息携带任务，永不截断。"""
        result = _build_summary((user("x" * 1500),))
        assert "x" * 1500 in result

    def test_previous_summary_kept_full(self):
        """迁移：旧摘要完整进入下一次合并。"""
        summary_msg = compact_summary(1, body="长" * 3000)
        result = _build_summary((summary_msg,))
        assert "长" * 3000 in result

    def test_tool_message_digest_fields(self):
        """迁移：tool 摘要行带工具名/状态/ref/标量字段。"""
        content = json.dumps(
            {
                "success": True,
                "raw_data": {
                    "__persisted_output__": True,
                    "ref_id": "$ref:parse_document:1",
                    "title": "通知",
                    "pages": 3,
                },
            }
        )
        digest = _summarize_tool_message(tool_msg(content, "t1", "parse_document"))
        assert "工具=parse_document" in digest
        assert "状态=成功" in digest
        assert "ref_id=$ref:parse_document:1" in digest

    def test_tool_digest_plain_and_failed(self):
        """迁移：纯文本与失败结果的摘要行。"""
        assert _summarize_tool_message(tool_msg("plain", "t1", "echo")).startswith("工具=echo")
        failed = json.dumps({"success": False, "raw_data": None, "error": "boom"})
        assert "状态=失败" in _summarize_tool_message(tool_msg(failed, "t1", "x"))
        omitted = json.dumps(
            {"success": True, "raw_data": {"_omitted": True, "ref_id": "$ref:search_documents:2"}}
        )
        assert "ref_id=$ref:search_documents:2" in _summarize_tool_message(
            tool_msg(omitted, "t1", "search_documents")
        )


class TestFindLastRealUser:
    def test_skips_reminders_and_empty_content(self):
        """迁移：提醒（pre-turn 与 periodic）与空内容不算真实用户消息。"""
        real = user("审核这份公文")
        msgs = [
            system("Sys"),
            real,
            assistant("Think"),
            reminder_msg(PRE_TURN_REMINDER),
            user(""),
        ]
        assert _find_last_real_user(msgs) is real
        assert _find_last_real_user(
            [real, reminder_msg(PERIODIC_REMINDER)]
        ) is real

    def test_only_reminders_returns_none(self):
        assert _find_last_real_user([reminder_msg(PRE_TURN_REMINDER)]) is None


class TestAlignToolBoundaries:
    @staticmethod
    def _seq() -> list[Message]:
        return [
            assistant("plan", tool_calls=(tool_call("t1"),)),
            tool_msg("r1", "t1", "tool1"),
            assistant("done"),
        ]

    def test_leading_orphan_tool_messages_dropped(self):
        """迁移：切片开头的孤儿 tool 消息被丢弃。"""
        msgs = [tool_msg("r1", "t1", "tool1"), user("next")]
        assert [m.role for m in _align_tool_boundaries(msgs)] == ["user"]

    def test_dangling_tool_calls_stripped(self):
        """迁移：无应答的 tool_calls 被剥离，有应答的保留。"""
        a = assistant("", tool_calls=(tool_call("t1"), tool_call("t2")))
        aligned = _align_tool_boundaries([a, tool_msg("r1", "t1", "tool1")])
        assert len(aligned) == 2
        assert [tc.id for tc in aligned[0].tool_calls] == ["t1"]

    def test_assistant_with_only_dangling_calls_dropped(self):
        """迁移：只剩悬空调用且无内容的 assistant 整条丢弃。"""
        a = assistant("", tool_calls=(tool_call("t9"),))
        assert [m.role for m in _align_tool_boundaries([a, user("next")])] == ["user"]

    def test_assistant_with_dangling_calls_but_content_kept_as_text(self):
        """迁移：悬空调用但有正文 → 剥离调用保留正文。"""
        a = assistant("一些推理", tool_calls=(tool_call("t9"),))
        aligned = _align_tool_boundaries([a])
        assert aligned[0].content == "一些推理"
        assert aligned[0].tool_calls == ()

    def test_aligned_sequence_unchanged(self):
        """迁移：本已对齐的序列原样返回。"""
        assert _align_tool_boundaries(self._seq()) == self._seq()

    async def test_fallback_slice_never_starts_with_orphan_tool(self, mgr, mock_model):
        """迁移：fallback 切片从工具边界对齐后不再有孤儿/悬空。"""
        mock_model.generate.side_effect = RuntimeError("LLM unavailable")
        pairs: list[Message] = []
        for i in range(5):
            pairs.append(assistant(f"plan {i}", tool_calls=(tool_call(f"t{i}", f"tool{i}"),)))
            pairs.append(tool_msg(f"result {i} " + "x" * 200, f"t{i}", f"tool{i}"))
        msgs = (
            system("Sys"),
            user("任务 " * 1500),
            *pairs,
            user("补充说明"),
        )
        result = await mgr.compact_if_needed(msgs)
        assert result[0].role != "tool"
        answered = {m.tool_call_id for m in result if m.role == "tool"}
        for m in result:
            if m.role == "assistant" and m.tool_calls:
                assert {tc.id for tc in m.tool_calls} <= answered


class TestFitTextToTokenBudget:
    def test_cjk_and_ascii_fitting(self):
        """迁移：CJK 按预算截断，带省略标记；预算内原样。"""
        text = "汉" * 100
        assert _fit_text_to_token_budget(text, 65) == text
        fitted = _fit_text_to_token_budget(text, 32)
        assert fitted.endswith("...[truncated]")
        assert len(fitted) < len(text)

    def test_empty_text(self):
        assert _fit_text_to_token_budget("", 10) == ""


class TestSplitCompactTemplate:
    def test_split_on_first_separator(self):
        """迁移：按首个 --- 分割；无分隔符用通用指令。"""
        assert _split_compact_template("A\n---\nB\n---\nC") == ("A", "B\n---\nC")
        assert _split_compact_template("只有系统部分") == ("只有系统部分", "Output the summary.")


# ===========================================================================
# §5 CompactState persistence & fork
# ===========================================================================


class TestStateAndFork:
    def test_default_state(self):
        """迁移：CompactState 默认值。"""
        from courtier.agent.core.context_manager import CompactState

        state = CompactState()
        assert state.has_compacted is False
        assert state.last_summary is None
        assert state.compact_count == 0

    def test_snapshot_load_roundtrip(self, mgr, mock_model, tmp_path):
        """迁移：snapshot → 全新 manager load 往返。"""
        mgr.state.has_compacted = True
        mgr.state.last_summary = "之前的摘要"
        mgr.state.compact_count = 3

        snap = mgr.snapshot_state()
        fresh = ContextManager(model=mock_model, cache_dir=str(tmp_path / "other"))
        fresh.load_state(snap)
        assert fresh.state.has_compacted is True
        assert fresh.state.last_summary == "之前的摘要"
        assert fresh.state.compact_count == 3

    def test_snapshot_has_version(self, mgr):
        """迁移：快照带版本号。"""
        assert mgr.snapshot_state()["version"] == 1

    def test_load_none_empty_and_unknown_version_keep_defaults(self, mgr):
        """迁移：None/空/未知版本 → 保持默认新状态。"""
        mgr.load_state(None)
        mgr.load_state({})
        mgr.load_state("not a dict")  # type: ignore[arg-type]
        mgr.load_state({"version": 999, "has_compacted": True, "compact_count": 7})
        assert mgr.state.has_compacted is False
        assert mgr.state.compact_count == 0

    def test_load_state_coerces_lenient_types(self, mgr):
        """5.1 (边界·pin) 异常类型按宽松规则转换：bool()/int() 直接生效。"""
        mgr.load_state({"version": 1, "has_compacted": "yes", "compact_count": "3"})
        assert mgr.state.has_compacted is True
        assert mgr.state.compact_count == 3
        mgr.load_state({"version": 1, "compact_count": -2})
        assert mgr.state.compact_count == -2

    def test_load_state_partial_payload_keeps_defaults(self, mgr):
        """5.2 (边界) 部分字段缺失 → 缺省填充。"""
        mgr.load_state({"version": 1})
        assert mgr.state.has_compacted is False
        assert mgr.state.last_summary is None
        assert mgr.state.compact_count == 0

    def test_over_budget_flag_not_persisted_across_restart(self, mgr, tmp_path):
        """5.3 (pin) last_compact_over_budget 不入快照 → 重启后复位。

        已知取舍：flag 只服务前端当次警告，跨请求重置为"未知"是可接受的。
        """
        mgr.state.last_compact_over_budget = True
        snap = mgr.snapshot_state()
        assert "last_compact_over_budget" not in snap
        fresh = ContextManager(cache_dir=str(tmp_path / "fresh"))
        fresh.load_state(snap)
        assert fresh.state.last_compact_over_budget is False

    def test_context_manager_fork_stays_plain(self, mgr, mock_model):
        """5.5 (回归) ContextManager.fork() 返回普通 ContextManager（文档化行为）。"""
        mgr.state.has_compacted = True
        mgr.state.compact_count = 5
        child = mgr.fork()
        assert type(child) is ContextManager
        assert child._cache is mgr._cache
        assert child._model is mgr._model
        assert child.state.has_compacted is False
        assert child.state.compact_count == 0
        assert child.max_context_tokens == mgr.max_context_tokens
        assert child.micro_compact_tokens == mgr.micro_compact_tokens
        assert child.compact_target_tokens == mgr.compact_target_tokens
        assert child.recent_tool_results_tokens == mgr.recent_tool_results_tokens
        assert child.large_output_threshold == mgr.large_output_threshold

    def test_fork_propagates_templates(self, mock_model, tmp_path):
        """迁移：fork 传播压缩/合并模板。"""
        mgr = ContextManager(
            model=mock_model,
            cache_dir=str(tmp_path / "c"),
            compact_prompt_template="T1 {history}",
            compact_merge_prompt_template="T2 {previous_summary} {new_segment}",
        )
        child = mgr.fork()
        assert child._compact_prompt_template == "T1 {history}"
        assert child._compact_merge_prompt_template == "T2 {previous_summary} {new_segment}"

    def test_memory_manager_fork_isolated_namespace(self, make_memory_manager):
        """5.4 (修复验证 F3) fork(sub_name) 返回 MemoryManager 且命名空间隔离。"""
        parent = make_memory_manager(session_id="sess_x")
        child = parent.fork(sub_name="h1")

        assert isinstance(child, MemoryManager)
        assert child.session_id == "sess_x:sub:h1"
        assert child._cache is parent._cache
        assert child._memory_store is parent._memory_store
        assert child.max_context_tokens == parent.max_context_tokens
        assert child.state.has_compacted is False
        assert child.state.compact_count == 0
        assert child._working_summary is None

    def test_nested_fork_chains_namespace(self, make_memory_manager):
        """5.6 (修复验证 F3) 嵌套 fork 命名空间自然成链。"""
        parent = make_memory_manager(session_id="sess_x")
        child = parent.fork(sub_name="h1")
        grandchild = child.fork(sub_name="h2")
        assert grandchild.session_id == "sess_x:sub:h1:sub:h2"

    def test_memory_manager_fork_without_sub_name_shares_namespace(
        self, make_memory_manager
    ):
        """5.4 补充：sub_name=None 回退为继承父命名空间（无标识直调场景）。"""
        parent = make_memory_manager(session_id="sess_x")
        child = parent.fork()
        assert child.session_id == "sess_x"
