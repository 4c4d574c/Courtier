"""Memory tools (memory_save/get/delete/recall) tests.

The tools receive the run-scoped context_manager from the registry at
dispatch time; these tests call execute() directly with a MemoryManager
(sub-agent fork inheritance is exercised via TestForkIsolation in
test_memory_manager.py — the tools see whatever manager the registry
hands them).
"""

from __future__ import annotations

import pytest

from courtier.agent.core.memory_manager import MemoryManager
from courtier.agent.tools.builtin.memory import (
    MemoryDeleteTool,
    MemoryGetTool,
    MemoryListTool,
    MemoryRecallTool,
    MemorySaveTool,
)


def _noop_progress(progress: dict) -> None:
    return


@pytest.fixture
def manager(tmp_path):
    return MemoryManager(cache_dir=str(tmp_path / "cache"), session_id="sess_t")


class TestMemorySaveTool:
    async def test_save_to_session_scope(self, manager):
        result = await MemorySaveTool().execute(
            on_progress=_noop_progress,
            context_manager=manager,
            key="user_style",
            value={"tone": "正式", "cites": True},
        )
        assert result.success is True
        assert await manager.session_get("user_style") == {"tone": "正式", "cites": True}

    async def test_save_to_long_term_scope(self, manager, tmp_path):
        result = await MemorySaveTool().execute(
            on_progress=_noop_progress,
            context_manager=manager,
            key="rule",
            value="引用来源",
            scope="long_term",
        )
        assert result.success is True
        other = MemoryManager(cache_dir=str(tmp_path / "cache"), session_id="sess_other")
        assert await other.long_term_get("rule") == "引用来源"

    async def test_requires_key(self, manager):
        result = await MemorySaveTool().execute(
            on_progress=_noop_progress, context_manager=manager, key="", value="x"
        )
        assert result.success is False

    async def test_unsupported_manager_fails_gracefully(self):
        result = await MemorySaveTool().execute(
            on_progress=_noop_progress, context_manager=object(), key="k", value="v"
        )
        assert result.success is False


class TestMemoryGetTool:
    async def test_roundtrip_and_missing(self, manager):
        await manager.session_set("k", {"n": 1})
        found = await MemoryGetTool().execute(
            on_progress=_noop_progress, context_manager=manager, key="k"
        )
        assert found.success is True
        assert found.data == {"found": True, "key": "k", "scope": "session", "value": {"n": 1}}

        missing = await MemoryGetTool().execute(
            on_progress=_noop_progress, context_manager=manager, key="nope"
        )
        assert missing.success is True
        assert missing.data["found"] is False


class TestMemoryDeleteTool:
    async def test_delete_existing_and_missing(self, manager):
        await manager.session_set("k", "v")
        result = await MemoryDeleteTool().execute(
            on_progress=_noop_progress, context_manager=manager, key="k"
        )
        assert result.success is True
        assert await manager.session_get("k") is None
        missing = await MemoryDeleteTool().execute(
            on_progress=_noop_progress, context_manager=manager, key="k"
        )
        assert missing.success is True  # silent no-op


class TestMemoryRecallTool:
    async def test_recall_matches_and_scoring(self, manager):
        await manager.session_set("fact", "本季度报告显示收入增长")
        await manager.long_term_set("style", "use formal tone")
        result = await MemoryRecallTool().execute(
            on_progress=_noop_progress, context_manager=manager, query="季度报告", top_k=5
        )
        assert result.success is True
        assert result.data["count"] >= 1
        assert result.data["matches"][0]["key"] == "fact"

    async def test_scope_filter(self, manager):
        await manager.session_set("s", "session data alpha")
        await manager.long_term_set("l", "long term data alpha")
        result = await MemoryRecallTool().execute(
            on_progress=_noop_progress,
            context_manager=manager,
            query="data alpha",
            scope="long_term",
        )
        assert [m["key"] for m in result.data["matches"]] == ["l"]

    async def test_requires_query(self, manager):
        result = await MemoryRecallTool().execute(
            on_progress=_noop_progress, context_manager=manager, query=""
        )
        assert result.success is False


class TestAgentAndRegistryWiring:
    def test_memory_tools_registered_on_agent(self, tmp_path):
        """Agent 对支持记忆层的 context manager 注册 4 个 memory 工具。"""
        from courtier.agent.agents.base import Agent
        from courtier.agent.testing import MockModelClient

        agent = Agent(name="T", role="r", tools=[], model=MockModelClient())
        agent._ensure_builtin_memory_tools()
        names = {t.name for t in agent.tool_registry.list_tools()}
        assert {"memory_save", "memory_get", "memory_delete", "memory_recall"} <= names

    async def test_registry_passes_context_manager_to_tools(self, tmp_path):
        """registry 把 run 作用域的 context_manager 注入工具——子代理 fork
        的隔离命名空间自动生效（写 child，parent 不可见）。"""
        from courtier.agent.tools.registry import ToolRegistry

        registry = ToolRegistry()
        registry.register(MemorySaveTool())
        parent = MemoryManager(cache_dir=str(tmp_path / "cache"), session_id="sess_p")
        child = parent.fork(sub_name="h1")

        result = await registry.execute(
            "memory_save", context_manager=child, key="k", value="from-child"
        )
        assert result.success is True
        assert await child.session_get("k") == "from-child"
        assert await parent.session_get("k") is None


class TestMemoryListTool:
    async def test_lists_both_tiers_with_previews(self, manager):
        await manager.session_set("s_key", {"tone": "正式"})
        await manager.long_term_set("l_key", "长" * 200)
        result = await MemoryListTool().execute(
            on_progress=_noop_progress, context_manager=manager
        )
        assert result.success is True
        items = result.data["items"]
        assert result.data["count"] == 2
        by_tier = {i["tier"]: i for i in items}
        assert by_tier["session"]["key"] == "s_key"
        assert "正式" in by_tier["session"]["preview"]
        # long-term preview truncated to 120 chars + ellipsis
        assert len(by_tier["long_term"]["preview"]) == 121

    async def test_scope_filter_and_empty(self, manager):
        await manager.session_set("s_key", "v")
        only_session = await MemoryListTool().execute(
            on_progress=_noop_progress, context_manager=manager, scope="session"
        )
        assert [i["tier"] for i in only_session.data["items"]] == ["session"]

        empty = await MemoryListTool().execute(
            on_progress=_noop_progress, context_manager=manager, scope="long_term"
        )
        assert empty.data == {"items": [], "count": 0}


class TestRecallEmptyTermsFails:
    async def test_wildcard_query_fails_loudly(self, manager):
        """'*' 提取不出词项 → 明确报错而不是误导性的空成功。"""
        await manager.long_term_set("gb_standard", "本季度验收标准")
        result = await MemoryRecallTool().execute(
            on_progress=_noop_progress, context_manager=manager, query="*"
        )
        assert result.success is False
        assert "memory_list" in (result.error or "")

    async def test_real_terms_with_no_match_still_succeeds_empty(self, manager):
        """有有效词项但确实无匹配 → 成功 + 空结果（语义正确）。"""
        result = await MemoryRecallTool().execute(
            on_progress=_noop_progress, context_manager=manager, query="完全无关的词项xyz"
        )
        assert result.success is True
        assert result.data["count"] == 0
