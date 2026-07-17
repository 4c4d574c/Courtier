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
