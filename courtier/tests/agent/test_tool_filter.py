"""Tests for Agent tool visibility filter (domain gating)."""

from __future__ import annotations

import pytest

from courtier.agent.agents.base import Agent
from courtier.agent.testing import MockModelClient
from courtier.agent.tools.protocol import ToolResult
from courtier.agent.tools.registry import ToolRegistry


class _PluginClient:
    """Minimal stand-in for a JSON-RPC plugin client."""

    def __init__(self, plugin_name: str) -> None:
        self.plugin_name = plugin_name


class _PluginTool:
    """Fake plugin-proxy tool carrying a _client with plugin_name."""

    name: str = "plugin_tool"
    description: str = "A fake plugin tool"
    parameters: dict = {"type": "object", "properties": {}}

    def __init__(self, name: str, plugin_name: str) -> None:
        self.name = name
        self._client = _PluginClient(plugin_name)

    async def execute(self, **kwargs):
        return ToolResult(success=True, data={})


class _BuiltinTool:
    """Fake builtin tool — no _client at all."""

    name: str = "builtin_tool"
    description: str = "A fake builtin tool"
    parameters: dict = {"type": "object", "properties": {}}

    async def execute(self, **kwargs):
        return ToolResult(success=True, data={})


def _domain_filter(shared_plugins: set[str], active_domains: set[str]):
    """Mirror of the DomainActivator.visible rule."""

    def visible(tool) -> bool:
        client = getattr(tool, "_client", None)
        plugin_name = getattr(client, "plugin_name", None)
        if plugin_name is None:
            return True  # builtin / SkillTool always pass
        return plugin_name in shared_plugins or plugin_name in active_domains

    return visible


@pytest.fixture
def shared_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(_PluginTool("shared_convert", "anydoc"))
    reg.register(_PluginTool("domain_parse", "docaudit"))
    reg.register(_BuiltinTool())
    return reg


class TestConstructorFiltering:
    def test_hidden_domain_tools_are_not_copied(self, shared_registry):
        """With no active domains, only shared plugins and builtins surface."""
        agent = Agent(
            name="Orch",
            role="orchestrator",
            tool_registry=shared_registry,
            model=MockModelClient(),
            tool_filter=_domain_filter({"anydoc"}, set()),
        )
        names = {t.name for t in agent.tool_registry.list_tools()}
        assert names == {"shared_convert", "builtin_tool", "read", "edit", "write"}
        assert "domain_parse" not in names

    def test_activated_domain_tools_surface(self, shared_registry):
        agent = Agent(
            name="Orch",
            role="orchestrator",
            tool_registry=shared_registry,
            model=MockModelClient(),
            tool_filter=_domain_filter({"anydoc"}, {"docaudit"}),
        )
        names = {t.name for t in agent.tool_registry.list_tools()}
        assert names == {"shared_convert", "domain_parse", "builtin_tool", "read", "edit", "write"}

    def test_explicit_tools_list_is_filtered_too(self, shared_registry):
        builtin = _BuiltinTool()
        hidden = _PluginTool("hidden_meta", "some_domain")
        agent = Agent(
            name="Orch",
            role="orchestrator",
            tools=[builtin, hidden],
            model=MockModelClient(),
            tool_filter=_domain_filter(set(), set()),
        )
        names = {t.name for t in agent.tool_registry.list_tools()}
        assert names == {"builtin_tool", "read", "edit", "write"}
        assert "hidden_meta" not in names

    def test_no_filter_keeps_everything(self, shared_registry):
        agent = Agent(
            name="Orch",
            role="orchestrator",
            tool_registry=shared_registry,
            model=MockModelClient(),
        )
        names = {t.name for t in agent.tool_registry.list_tools()}
        assert names == {"shared_convert", "domain_parse", "builtin_tool", "read", "edit", "write"}


class TestRunSyncFiltering:
    @pytest.mark.asyncio
    async def test_new_plugin_tool_hidden_until_domain_active(self, shared_registry):
        agent = Agent(
            name="Orch",
            role="orchestrator",
            tool_registry=shared_registry,
            model=MockModelClient(tool_calls=[]),
            tool_filter=_domain_filter({"anydoc"}, set()),
        )
        # A domain plugin registers after construction — still hidden.
        late_tool = _PluginTool("domain_audit", "docaudit")
        shared_registry.register(late_tool)
        await agent.run("noop")
        assert "domain_audit" not in {t.name for t in agent.tool_registry.list_tools()}

        # The filter closure reads live state: activating the domain (mutating
        # the same set the closure captured) surfaces the tool on the next run.
        active: set[str] = set()
        agent._tool_filter = _domain_filter({"anydoc"}, active)
        active.add("docaudit")
        await agent.run("noop")
        names = {t.name for t in agent.tool_registry.list_tools()}
        assert "domain_audit" in names

    @pytest.mark.asyncio
    async def test_plugin_restart_hot_swap_still_works_under_filter(self, shared_registry):
        agent = Agent(
            name="Orch",
            role="orchestrator",
            tool_registry=shared_registry,
            model=MockModelClient(tool_calls=[]),
            tool_filter=_domain_filter({"anydoc"}, set()),
        )

        # Plugin restart: the shared registry holds a fresh proxy with a new
        # client for the same tool name.
        replacement = _PluginTool("shared_convert", "anydoc")
        shared_registry.register(replacement, force=True)
        await agent.run("noop")

        held = agent.tool_registry.get("shared_convert")
        assert held is replacement
        assert getattr(held, "_client", None) is replacement._client

    @pytest.mark.asyncio
    async def test_filter_exception_fails_closed(self, shared_registry):
        def broken_filter(tool) -> bool:
            raise RuntimeError("boom")

        agent = Agent(
            name="Orch",
            role="orchestrator",
            tool_registry=shared_registry,
            model=MockModelClient(),
            tool_filter=broken_filter,
        )
        # Fail-closed: no plugin/domain tools surface. The universal file
        # primitives remain — they bypass tool_filter by design and are
        # bounded by the permission gate instead.
        assert {t.name for t in agent.tool_registry.list_tools()} == {
            "read",
            "edit",
            "write",
        }
