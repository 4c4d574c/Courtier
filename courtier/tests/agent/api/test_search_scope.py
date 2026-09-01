"""Tests for resource-library visibility enforcement in search plugin and ScopedTool."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

# Load the search plugin's tools.py by path (it lives outside the package tree).
_TOOLS_PATH = Path(__file__).resolve().parents[3] / "plugins" / "shared" / "search" / "tools.py"
_spec = importlib.util.spec_from_file_location("search_plugin_tools", _TOOLS_PATH)
tools = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tools)


class TestBuildEsQueryOwnerScope:
    def test_unset_scope_has_no_visibility_filter(self):
        body = tools._build_es_query("通知")
        assert "filter" not in body["query"]["bool"]

    def test_int_scope_filters_public_and_own(self):
        body = tools._build_es_query("通知", owner_scope=7)
        (clause,) = body["query"]["bool"]["filter"]
        should = clause["bool"]["should"]
        assert {"term": {"visibility": "public"}} in should
        assert {"term": {"owner_id": 7}} in should
        # Legacy chunks without the visibility field count as public.
        assert any("must_not" in s.get("bool", {}) for s in should)
        assert clause["bool"]["minimum_should_match"] == 1

    def test_none_scope_is_public_only(self):
        body = tools._build_es_query("通知", owner_scope=None)
        (clause,) = body["query"]["bool"]["filter"]
        should = clause["bool"]["should"]
        assert {"term": {"visibility": "public"}} in should
        assert not any("owner_id" in s.get("term", {}) for s in should)

    def test_scope_combines_with_other_filters(self):
        body = tools._build_es_query("通知", doc_type="通知", owner_scope=3)
        filters = body["query"]["bool"]["filter"]
        assert len(filters) == 2
        assert {"term": {"doc_type": "通知"}} in filters


class TestScopedTool:
    def test_injection_wins_over_model_kwargs(self):
        from courtier.agent.tools.scoped import ScopedTool

        captured = {}

        class Inner:
            name = "search_documents"
            display_name = "搜索文档"
            description = "desc"
            parameters = {"type": "object"}

            async def execute(self, **kwargs):
                captured.update(kwargs)
                return "ok"

        scoped = ScopedTool(Inner(), {"_owner_scope": 7})
        assert scoped.name == "search_documents"
        assert scoped.display_name == "搜索文档"
        assert scoped.parameters == {"type": "object"}

        import asyncio

        result = asyncio.run(scoped.execute(query="x", _owner_scope=999, limit=5))
        assert result == "ok"
        assert captured["_owner_scope"] == 7  # model-supplied value overridden
        assert captured["query"] == "x"
        assert captured["limit"] == 5

    def test_apply_owner_scope_wraps_only_sensitive_tools(self):
        from courtier.agent.api.services.agent_service import apply_owner_scope
        from courtier.agent.tools.registry import ToolRegistry

        def fake_tool(name):
            return SimpleNamespace(
                name=name,
                description="",
                display_name=None,
                skill="",
                parameters={"type": "object", "properties": {}},
                output_schema=None,
                skip_persist=True,
                output_content_type=None,
                input_contract=None,
                output_contract=None,
                runtime_policy=None,
            )

        registry = ToolRegistry()
        registry.register(fake_tool("search_documents"))
        registry.register(fake_tool("parse_layout"))
        agent = SimpleNamespace(tool_registry=registry)

        apply_owner_scope(agent, 7)

        wrapped = registry.get("search_documents")
        assert type(wrapped).__name__ == "ScopedTool"
        assert wrapped._injections == {"_owner_scope": 7}
        assert type(registry.get("parse_layout")).__name__ != "ScopedTool"

        # Anonymous caller: injected as None (public-only), still wrapped.
        apply_owner_scope(agent, None)
        assert registry.get("search_documents")._injections == {"_owner_scope": None}
