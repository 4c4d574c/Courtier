"""Cross-session isolation of owner-scope injection.

Regression for the shared-registry ScopedTool nesting leak: applying
owner scopes on the app-wide registry nested wrappers (innermost injection
wins), pinning the first session's owner for every later session.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from courtier.agent.api.services.agent_service import apply_owner_scope
from courtier.agent.tools.registry import ToolRegistry
from courtier.agent.tools.scoped import ScopedTool


class _RecordingTool:
    """Stub search tool capturing the ``_owner_scope`` it is executed with."""

    name = "search_documents"
    display_name = None
    description = ""
    parameters = {"type": "object", "properties": {}}
    output_schema = None
    skill = ""
    skip_persist = True
    output_content_type = None
    input_contract = None
    output_contract = None
    runtime_policy = None

    def __init__(self) -> None:
        self.seen_scopes: list = []

    async def execute(self, **kwargs):
        self.seen_scopes.append(kwargs.get("_owner_scope"))
        return SimpleNamespace(success=True, data={"ok": True}, error=None, metadata={})


def _wrap_depth(registry: ToolRegistry) -> int:
    node = registry.get("search_documents")
    depth = 0
    while isinstance(node, ScopedTool):
        depth += 1
        node = node._inner
    return depth


class TestApplyOwnerScopeIsolation:
    def test_repeated_apply_never_nests_wrappers(self):
        base = ToolRegistry()
        raw = _RecordingTool()
        base.register(raw)

        clone = base.clone()
        agent = SimpleNamespace(tool_registry=clone)
        for owner in (1, 2, 3, 4, 5):
            apply_owner_scope(agent, owner)

        assert _wrap_depth(clone) == 1
        assert clone.get("search_documents")._inner is raw
        assert clone.get("search_documents")._injections == {"_owner_scope": 5}

    @pytest.mark.asyncio
    async def test_sessions_execute_with_their_own_scope(self):
        base = ToolRegistry()
        raw = _RecordingTool()
        base.register(raw)

        # Two per-session clones, each scoped to a different owner.
        reg_a, reg_b = base.clone(), base.clone()
        apply_owner_scope(SimpleNamespace(tool_registry=reg_a), 1)
        apply_owner_scope(SimpleNamespace(tool_registry=reg_b), 2)

        await reg_a.get("search_documents").execute(query="x")
        await reg_b.get("search_documents").execute(query="y")

        assert raw.seen_scopes == [1, 2]

    @pytest.mark.asyncio
    async def test_concurrent_sessions_do_not_cross_contaminate(self):
        base = ToolRegistry()
        raw = _RecordingTool()
        base.register(raw)

        reg_a, reg_b = base.clone(), base.clone()
        apply_owner_scope(SimpleNamespace(tool_registry=reg_a), 1)
        apply_owner_scope(SimpleNamespace(tool_registry=reg_b), 2)

        # Interleaved execution: whichever finishes first, each call must
        # carry its own session's scope.
        await asyncio.gather(
            reg_a.get("search_documents").execute(query="a"),
            reg_b.get("search_documents").execute(query="b"),
        )

        assert sorted(s for s in raw.seen_scopes if s is not None) == [1, 2]

    def test_base_registry_never_mutated(self):
        base = ToolRegistry()
        base.register(_RecordingTool())

        clone = base.clone()
        apply_owner_scope(SimpleNamespace(tool_registry=clone), 7)

        base_entry = base.get("search_documents")
        assert not isinstance(base_entry, ScopedTool)


class _DummySettings:
    llm_base_url = "http://localhost:8000/v1"
    llm_api_key = "EMPTY"
    llm_model = "mock"
    llm_temperature = 0.0
    llm_max_tokens = 0
    llm_timeout = 180.0
    llm_extra_body = None
    llm_frequency_penalty = 0.0
    llm_presence_penalty = 0.0
    cache_dir = ".agent_cache"
    es_hosts = ""
    es_index_results = "docaudit_results"
    subagent_max_runtime_seconds = 300.0
    subagent_max_cumulative_runtime_seconds = 600.0
    subagent_max_turns = 20
    subagent_max_depth = 5
    subagent_max_total_spawns = 20


class TestBuildAgentSessionRegistry:
    @pytest.mark.asyncio
    async def test_build_agent_scopes_clone_not_base(self):
        from courtier.agent.api.services.agent_service import build_agent

        base = ToolRegistry()
        raw = _RecordingTool()
        base.register(raw)

        agent, _, _ = await build_agent(
            settings=_DummySettings(),
            tool_registry=base,
            owner_id=7,
        )

        assert agent.tool_registry is not base
        wrapped = agent.tool_registry.get("search_documents")
        assert isinstance(wrapped, ScopedTool)
        assert wrapped._injections == {"_owner_scope": 7}
        # The app-wide registry keeps the raw tool: admin/management callers
        # and later sessions are unaffected.
        assert base.get("search_documents") is raw
