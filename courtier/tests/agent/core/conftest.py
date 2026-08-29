"""Shared fixtures for the context management test suite.

Covers ContextManager (estimate / three layers / state / fork) and
MemoryManager per docs/architecture/context-manager-test-plan.md.  Every
test runs offline: models are mocks, disk is tmp_path-scoped.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from courtier.agent.core.context_manager import ContextManager
from courtier.agent.core.state import Message
from courtier.agent.core.tool_call import ToolCall
from courtier.prompts.engine import PromptEngine

# Reminder texts rendered from the core default bundles — the single source
# of truth for what the agent loop injects (source="reminder").
_ENGINE = PromptEngine.from_domain_directories([], locale="zh-CN")
PRE_TURN_REMINDER = _ENGINE.render("behavioral.pre_turn_reminder")
PERIODIC_REMINDER = _ENGINE.render("behavioral.periodic_reminder")

# Budget knobs sized so tests exercise compaction without huge fixtures:
# the micro gate is always open (1 token) and the recency budget keeps
# roughly two tiny tool results.
DEFAULT_BUDGETS: dict[str, int] = {
    "max_context_tokens": 2000,
    "micro_compact_tokens": 1,
    "compact_target_tokens": 1500,
    "recent_tool_results_tokens": 10,
    "large_output_threshold": 500,
}


# -- Message constructors ----------------------------------------------------

def user(content: str | None = None, **kw) -> Message:
    return Message(role="user", content=content, **kw)


def assistant(content: str | None = None, tool_calls: tuple = (), **kw) -> Message:
    return Message(role="assistant", content=content, tool_calls=tool_calls, **kw)


def system(content: str, **kw) -> Message:
    return Message(role="system", content=content, **kw)


def tool_msg(content: str, call_id: str, name: str, **kw) -> Message:
    return Message(role="tool", content=content, tool_call_id=call_id, name=name, **kw)


def reminder_msg(content: str, **kw) -> Message:
    return Message(role="user", content=content, source="reminder", **kw)


def tool_call(call_id: str, name: str = "tool", arguments: dict | None = None) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=arguments or {})


def compact_summary(count: int, body: str = "Compacted summary.") -> Message:
    """A summary message exactly as _full_compact writes it."""
    return Message(
        role="system",
        content=f"[上下文压缩 #{count}] 以下为之前对话的摘要，请继续完成任务：\n\n{body}",
    )


# -- Fixtures ----------------------------------------------------------------

@pytest.fixture
def mock_model():
    model = MagicMock()
    model.generate = AsyncMock(
        return_value=MagicMock(content="Compacted summary.", tool_calls=[])
    )
    return model


@pytest.fixture
def make_manager(mock_model, tmp_path):
    """ContextManager factory with default test budgets; override any kwarg."""

    def _make(**overrides) -> ContextManager:
        return ContextManager(
            model=mock_model,
            cache_dir=str(tmp_path / ".agent_cache"),
            **{**DEFAULT_BUDGETS, **overrides},
        )

    return _make


@pytest.fixture
def mgr(make_manager) -> ContextManager:
    return make_manager()


@pytest.fixture
def make_memory_manager(mock_model, tmp_path):
    """MemoryManager factory on the same default budgets."""

    def _make(session_id: str = "sess_test", **overrides):
        from courtier.agent.core.memory_manager import MemoryManager

        return MemoryManager(
            model=mock_model,
            cache_dir=str(tmp_path / "cache"),
            session_id=session_id,
            **{**DEFAULT_BUDGETS, **overrides},
        )

    return _make
