"""MemoryManager suite (§6) — per docs/architecture/context-manager-test-plan.md.

Working tier (compaction / budgets) is covered by test_context_manager.py;
this file covers the session / long-term / retrieval tiers and their
interaction with the working summary.  Outline IDs cited in docstrings.
"""

from __future__ import annotations

import pytest

from courtier.agent.core.memory_manager import MemoryManager, MemoryQuery
from courtier.agent.core.state import Message


@pytest.fixture
def manager(tmp_path):
    return MemoryManager(cache_dir=str(tmp_path / "cache"), session_id="sess_1")


class TestMemoryTiers:
    async def test_session_get_set(self, manager):
        """6.7 (迁移) session 层基本读写。"""
        await manager.session_set("user_pref", {"lang": "zh"})
        assert await manager.session_get("user_pref") == {"lang": "zh"}

    async def test_session_isolated_by_session_id(self, manager, tmp_path):
        """6.7 (迁移) 不同 session_id 的 session 层互不可见。"""
        other = MemoryManager(cache_dir=str(tmp_path / "cache"), session_id="sess_2")
        await manager.session_set("key", "a")
        await other.session_set("key", "b")
        assert await manager.session_get("key") == "a"
        assert await other.session_get("key") == "b"

    async def test_long_term_persists_across_sessions(self, manager, tmp_path):
        """6.7 (迁移) long_term 层跨会话共享。"""
        await manager.long_term_set("rule", "always cite sources")
        other = MemoryManager(cache_dir=str(tmp_path / "cache"), session_id="sess_2")
        assert await other.long_term_get("rule") == "always cite sources"

    async def test_delete_missing_key_is_noop(self, manager):
        """6.1 删除不存在的 key 是 no-op，不抛异常。"""
        await manager.session_delete("nope")
        await manager.long_term_delete("nope")
        assert await manager.session_get("nope") is None

    async def test_keys_empty_then_populated(self, manager):
        """6.1 空 namespace 的 keys 为空列表，写入后可见。"""
        assert await manager.session_keys() == []
        assert await manager.long_term_keys() == []
        await manager.session_set("a", 1)
        assert await manager.session_keys() == ["a"]

    async def test_delete_removes_value(self, manager):
        """6.1 删除后读取为 None。"""
        await manager.session_set("k", "v")
        await manager.session_delete("k")
        assert await manager.session_get("k") is None

    async def test_top_k_zero_and_negative(self, manager):
        """6.3 (边界·pin) top_k=0 → 空；top_k 为负 → Python 负切片怪癖（返回除末条外）。

        已知怪癖：占位实现直接 results[:top_k]，负值不崩但语义无意义；
        记录固化，待接入真实检索后端时一并修正。
        """
        for i in range(3):
            await manager.long_term_set(f"item_{i}", "value common")
        assert await manager.retrieve(MemoryQuery(text="value", top_k=0)) == []
        assert len(await manager.retrieve(MemoryQuery(text="value", top_k=-1))) == 2

    async def test_corrupt_memory_file_degrades_to_none(self, manager, tmp_path):
        """6.5 (极端) 记忆文件损坏 → 读取降级为 None，不崩；重写后恢复。"""
        await manager.session_set("doc", {"note": "value"})
        files = list((tmp_path / ".agent_memory").rglob("*.json"))
        assert files, "memory file should exist on disk"
        files[0].write_text("{corrupt json", encoding="utf-8")

        assert await manager.session_get("doc") is None

        await manager.session_set("doc", {"note": "fresh"})
        assert await manager.session_get("doc") == {"note": "fresh"}


class TestRetrieveQueryTerms:
    """6.2 (修复验证 F4) CJK bigram 查询切词的得分语义。"""

    async def test_chinese_partial_query_scores_proportionally(self, manager):
        """探针：整串子串匹配不到的部分命中必须按 bigram 比例得分。

        Old whitespace splitting made the whole CJK query one term, so a
        memory containing "季度报告" scored 0 against query "季度报告审核".
        """
        await manager.session_set("fact", "本季度报告显示收入增长")
        results = await manager.retrieve(MemoryQuery(text="季度报告审核"))
        assert results, "partial CJK overlap must still recall"
        assert results[0].score == pytest.approx(3 / 5)  # 季度/度报/报告 hit

    async def test_chinese_full_query_scores_one(self, manager):
        await manager.session_set("fact", "季度报告已归档")
        results = await manager.retrieve(MemoryQuery(text="季度报告"))
        assert results[0].score == pytest.approx(1.0)  # 季度/度报/报告 all hit

    async def test_single_cjk_character_query(self, manager):
        """Old len>1 filter dropped single CJK chars entirely; now kept."""
        await manager.session_set("fact", "需要人工审核")
        results = await manager.retrieve(MemoryQuery(text="审"))
        assert results and results[0].key == "fact"

    async def test_mixed_cjk_ascii_query(self, manager):
        await manager.session_set("m", "Q3 审核结论")
        results = await manager.retrieve(MemoryQuery(text="审核 Q3 报告"))
        assert results and results[0].score == pytest.approx(2 / 3)  # 审核+q3, 报告 miss

    async def test_ascii_whitespace_semantics_unchanged(self, manager):
        await manager.session_set("style", "use formal tone")
        await manager.session_set("other", "something else")
        results = await manager.retrieve(MemoryQuery(text="formal tone"))
        assert len(results) == 1
        assert results[0].key == "style"
        assert results[0].score == pytest.approx(1.0)

    async def test_empty_and_single_char_ascii_queries_recall_nothing(self, manager):
        await manager.session_set("fact", "plain content")
        assert await manager.retrieve(MemoryQuery(text="")) == []
        assert await manager.retrieve(MemoryQuery(text="   ")) == []
        assert await manager.retrieve(MemoryQuery(text="a b")) == []  # len>1 filter


class TestWorkingMemory:
    async def test_compact_updates_working_summary(self, manager):
        """6.4 压缩检查后 _working_summary 同步更新，可被检索命中。"""
        await manager.compact_if_needed(
            (Message(role="user", content="quarterly revenue report"),)
        )
        assert "quarterly revenue report" in (manager._working_summary or "")

        results = await manager.retrieve(MemoryQuery(text="quarterly"))
        assert any(r.tier == "working" for r in results)

    async def test_working_recall_uses_context_summary(self, manager):
        """6.7 (迁移) working_recall 以当前消息构建 working 摘要并检索。"""
        messages = (
            Message(role="user", content="Summarize the quarterly report."),
            Message(role="assistant", content="Revenue grew 12%."),
        )
        results = await manager.working_recall(messages, "quarterly revenue")
        assert len(results) == 1
        assert results[0].tier == "working"

    async def test_inherits_context_manager_compact(self, manager):
        """6.7 (迁移) 继承的三层压缩行为正常。"""
        from courtier.agent.testing import MockModelClient

        manager._model = MockModelClient()
        messages = tuple(Message(role="user", content="x") for _ in range(20))
        compacted = await manager.compact_if_needed(messages)
        assert isinstance(compacted, tuple)


class TestForkIsolation:
    async def test_fork_sub_name_isolates_session_memory_shares_long_term(
        self, manager, tmp_path
    ):
        """6.6 (修复验证 F3) fork 后 session 记忆与父隔离、long_term 双向共享。"""
        child = manager.fork(sub_name="h1")
        await manager.session_set("k", "parent")
        await child.session_set("k", "child")
        assert await manager.session_get("k") == "parent"
        assert await child.session_get("k") == "child"

        await child.long_term_set("rule", "cite sources")
        other = MemoryManager(cache_dir=str(tmp_path / "cache"), session_id="sess_other")
        assert await other.long_term_get("rule") == "cite sources"


class TestForkConcurrency:
    async def test_forked_children_concurrent_writes_stay_isolated(self, manager):
        """6.6 补充（并发）: 多个子代理并发写各自命名空间互不串扰；
        long_term 并发写为 last-writer-wins 且不损坏文件。"""
        import asyncio

        children = [manager.fork(sub_name=f"h{i}") for i in range(3)]

        async def write_session(child, i: int):
            for n in range(5):
                await child.session_set(f"key_{i}", f"child-{i}-{n}")

        await asyncio.gather(*[write_session(c, i) for i, c in enumerate(children)])

        for i, child in enumerate(children):
            assert await child.session_get(f"key_{i}") == f"child-{i}-4"
        assert await manager.session_get("key_0") is None  # parent namespace untouched

        async def write_long(i: int):
            await manager.long_term_set("shared", f"v{i}")

        await asyncio.gather(*[write_long(i) for i in range(6)])
        assert await manager.long_term_get("shared") in {f"v{i}" for i in range(6)}
