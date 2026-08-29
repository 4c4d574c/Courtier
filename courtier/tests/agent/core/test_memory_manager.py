"""Tests for MemoryManager four-tier memory."""

import pytest

from courtier.agent.core.memory_manager import MemoryManager, MemoryQuery


@pytest.fixture
def manager(tmp_path):
    return MemoryManager(cache_dir=str(tmp_path / "cache"), session_id="sess_1")


class TestMemoryManager:
    @pytest.mark.asyncio
    async def test_session_get_set(self, manager):
        await manager.session_set("user_pref", {"lang": "zh"})
        result = await manager.session_get("user_pref")
        assert result == {"lang": "zh"}

    @pytest.mark.asyncio
    async def test_session_is_isolated_by_session_id(self, manager, tmp_path):
        other = MemoryManager(
            cache_dir=str(tmp_path / "cache"), session_id="sess_2"
        )
        await manager.session_set("key", "a")
        await other.session_set("key", "b")
        assert await manager.session_get("key") == "a"
        assert await other.session_get("key") == "b"

    @pytest.mark.asyncio
    async def test_long_term_persists_across_sessions(self, manager, tmp_path):
        await manager.long_term_set("rule", "always cite sources")
        other = MemoryManager(
            cache_dir=str(tmp_path / "cache"), session_id="sess_2"
        )
        assert await other.long_term_get("rule") == "always cite sources"

    @pytest.mark.asyncio
    async def test_retrieve_returns_matching_memories(self, manager):
        await manager.session_set("file_path", "/docs/report.pdf")
        await manager.long_term_set("style", "use formal tone")
        results = await manager.retrieve(MemoryQuery(text="formal tone"))
        assert len(results) == 1
        assert results[0].key == "style"
        assert results[0].tier == "long_term"

    @pytest.mark.asyncio
    async def test_retrieve_limits_top_k(self, manager):
        for i in range(5):
            await manager.long_term_set(f"item_{i}", f"value {i}")
        results = await manager.retrieve(MemoryQuery(text="value", top_k=2))
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_retrieve_filters_by_tier(self, manager):
        await manager.session_set("s", "session data")
        await manager.long_term_set("l", "long term data")
        results = await manager.retrieve(MemoryQuery(text="data", tier="session"))
        assert len(results) == 1
        assert results[0].tier == "session"

    @pytest.mark.asyncio
    async def test_working_recall_uses_context_summary(self, manager):
        from courtier.agent.core.state import Message

        messages = (
            Message(role="user", content="Summarize the quarterly report."),
            Message(role="assistant", content="Revenue grew 12%."),
        )
        results = await manager.working_recall(messages, "quarterly revenue")
        assert len(results) == 1
        assert results[0].tier == "working"
        assert "quarterly" in results[0].value.lower()

    @pytest.mark.asyncio
    async def test_inherits_context_manager_compact(self, manager):
        from courtier.agent.core.state import Message
        from courtier.agent.testing import MockModelClient

        manager._model = MockModelClient()
        messages = tuple(Message(role="user", content="x") for _ in range(20))
        compacted = await manager.compact_if_needed(messages)
        # With a tiny threshold, compact_if_needed should still preserve system/user.
        assert isinstance(compacted, tuple)

    def test_estimate_tokens_inherited(self, manager):
        from courtier.agent.core.state import Message

        messages = (Message(role="user", content="hello"),)
        tokens = manager.estimate_tokens(messages)
        assert tokens > 0


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
