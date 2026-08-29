"""ContextManager suite — rewritten per docs/architecture/context-manager-test-plan.md.

Sections land incrementally (plan tasks 3–8); each test's docstring cites
its outline ID.  The legacy tests/agent/test_context_manager.py is deleted
once all of its coverage is re-homed here.
"""

from __future__ import annotations

import json

from .conftest import system, tool_msg, user


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
