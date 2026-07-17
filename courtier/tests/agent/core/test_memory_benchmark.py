"""Smoke tests for the long-session memory benchmark."""

import tempfile
from pathlib import Path

import pytest

from courtier.agent.core.memory_manager import MemoryManager, MemoryQuery
from courtier.agent.core.state import Message
from courtier.benchmarks.long_session_memory_benchmark import (
    approx_tokens,
    build_messages,
    build_messages_with_memory,
    run_turn,
)


def _user_msg(content: str) -> Message:
    return Message(role="user", content=content)


class TestApproxTokens:
    def test_positive_for_non_empty_text(self):
        assert approx_tokens("hello world") >= 1

    def test_one_for_short_text(self):
        # Very short ASCII text can collapse to 1 token in the rough estimator.
        assert approx_tokens("hi") == 1


class TestBuildMessages:
    @pytest.mark.asyncio
    async def test_memory_messages_are_smaller_than_baseline(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = MemoryManager(cache_dir=str(Path(tmpdir) / "cache"))
            history = []

            for i in range(5):
                await run_turn(i, manager, history, top_k=2)

            baseline = build_messages(history)
            with_memory = await build_messages_with_memory(manager, history, top_k=2)

            baseline_tokens = sum(
                approx_tokens(m.content or "") for m in baseline
            )
            memory_tokens = sum(
                approx_tokens(m.content or "") for m in with_memory
            )

            assert len(baseline) > len(with_memory)
            assert baseline_tokens > memory_tokens

    @pytest.mark.asyncio
    async def test_retrieve_finds_relevant_long_term_memory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = MemoryManager(cache_dir=str(Path(tmpdir) / "cache"))
            await manager.long_term_set("format_rule", "公文版头应包含发文字号")
            await manager.long_term_set("unrelated", "会议安排在下周三")

            results = await manager.retrieve(MemoryQuery(text="公文 发文字号", top_k=2))
            keys = {r.key for r in results}

            assert "format_rule" in keys
            assert "unrelated" not in keys
