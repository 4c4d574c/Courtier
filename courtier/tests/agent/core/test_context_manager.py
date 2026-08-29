"""ContextManager suite — rewritten per docs/architecture/context-manager-test-plan.md.

Sections land incrementally (plan tasks 3–8); each test's docstring cites
its outline ID.  The legacy tests/agent/test_context_manager.py is deleted
once all of its coverage is re-homed here.
"""

from __future__ import annotations

import json

from .conftest import assistant, system, tool_msg, user


class TestTokenEstimationAndCalibration:
    async def test_calibration_reset_after_full_compact(self, mgr):
        """1.8 (修复验证 F2) 全量压缩后校准值清空，budget_usage 回落为启发式。

        The calibration was measured against the pre-compaction tuple; it
        must not keep the post-compaction budget checks pinned high.
        """
        mgr.update_actual_usage(5000)
        msgs = tuple(user("x" * 3000) for _ in range(3))
        compacted = await mgr.compact_if_needed(msgs)

        assert mgr._last_actual_prompt_tokens is None
        assert mgr.budget_usage(compacted) == mgr.estimate_tokens(compacted)


class TestLayer3FullCompaction:
    async def test_no_merge_compaction_right_after_full_compact(self, mgr, mock_model):
        """4.10 (修复验证 F2) 压缩后新用户轮 + 小 estimate 不触发合并压缩。

        With the stale pre-compaction calibration this used to fire a
        wasteful summary-merge LLM call; a fresh provider report restores
        calibration-driven triggering.
        """
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


class TestLayer2MicroCompact:
    async def test_recompacting_placeholder_is_noop_and_keeps_ref(self, mgr):
        """3.3 (修复验证 F1) 已压缩占位符再次被压缩必须原样保留。

        The loop re-runs micro_compact over the full history every turn, so
        a placeholder that ages out of the recency window gets re-processed.
        Re-deriving its ref would drop the pointer (the placeholder's own
        top-level ref_id shape is not readable by _extract_ref_info), so the
        second pass must be a no-op.
        """
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
