"""Tests for agent_service wiring — unified domain-gated build_agent."""

from types import SimpleNamespace

import pytest

from courtier.agent.agents.orch import OrchestratorAgent
from courtier.agent.api.services.agent_service import build_agent, build_model_client
from courtier.agent.core.backends.router import ModelRouter
from courtier.agent.core.model import BackendModelClient
from courtier.agent.runtime import AgentRuntime
from courtier.agent.tools.builtin.activate_domain import ActivateDomainTool
from courtier.agent.tools.builtin.skill import SkillTool
from courtier.agent.tools.registry import ToolRegistry
from courtier.config import CourtierConfig


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


def _fake_plugin_tool(name: str, plugin_name: str):
    """Minimal tool double carrying the plugin origin like ProxyTool does."""
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
        _client=SimpleNamespace(plugin_name=plugin_name),
    )


class _FakePluginSystem:
    """plugin_domain mapping only — no real plugin processes."""

    def __init__(self) -> None:
        self._domain_by_plugin = {
            "search": None,
            "parse": "docaudit",
            "format_audit": "docaudit",
        }

    def plugin_domain(self, name: str):
        return self._domain_by_plugin.get(name)

    def get_scan_results(self):
        return {}

    def get_system_prompts(self):
        return {}


def _courtier_config() -> CourtierConfig:
    cfg = CourtierConfig.from_env()
    cfg.discover()
    return cfg


def _gated_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(_fake_plugin_tool("search_documents", "search"))
    reg.register(_fake_plugin_tool("parse_layout", "parse"))
    reg.register(_fake_plugin_tool("format_audit", "format_audit"))
    return reg


@pytest.mark.asyncio
async def test_build_agent_creates_orchestrator_with_runtime():
    reg = ToolRegistry()
    agent, context_manager, model_name = await build_agent(
        settings=_DummySettings(),
        tool_registry=reg,
    )

    assert isinstance(agent, OrchestratorAgent)
    assert isinstance(agent._agent_runtime, AgentRuntime)

    orchestrator_tools = {t.name for t in agent.tool_registry.list_tools()}
    assert "get_artifact" in orchestrator_tools
    assert "list_artifacts" in orchestrator_tools
    # The meta-tool rides along and its activator is wired.
    assert "activate_domain" in orchestrator_tools
    tool = agent.tool_registry.get("activate_domain")
    assert isinstance(tool, ActivateDomainTool)
    assert tool._activator is not None
    assert agent._domain_activator is not None


@pytest.mark.asyncio
async def test_build_agent_uses_disk_result_store_when_es_not_configured():
    reg = ToolRegistry()
    agent, _, _ = await build_agent(
        settings=_DummySettings(),
        tool_registry=reg,
    )

    assert agent._agent_runtime.artifact_store is not None


@pytest.mark.asyncio
async def test_build_agent_gates_domain_tools_by_default():
    """Domain-gating: without activation only shared plugins are visible."""
    reg = _gated_registry()
    agent, _, _ = await build_agent(
        settings=_DummySettings(),
        tool_registry=reg,
        courtier_config=_courtier_config(),
        plugin_system=_FakePluginSystem(),
        shared_plugin_names={"search"},
    )

    visible = {t.name for t in agent.tool_registry.list_tools()}
    assert "search_documents" in visible
    assert "parse_layout" not in visible  # docaudit domain tool hidden
    assert "format_audit" not in visible


@pytest.mark.asyncio
async def test_build_agent_replays_active_domains():
    """Persisted activation set is replayed silently on rebuild."""
    reg = _gated_registry()
    agent, _, _ = await build_agent(
        settings=_DummySettings(),
        tool_registry=reg,
        courtier_config=_courtier_config(),
        plugin_system=_FakePluginSystem(),
        shared_plugin_names={"search"},
        active_domains=("docaudit",),
    )

    assert agent._domain_activator.active_domains == {"docaudit"}
    visible = {t.name for t in agent.tool_registry.list_tools()}
    assert "parse_layout" in visible
    assert "format_audit" in visible
    # Domain skills are registered as SkillTools (real docaudit skills dir).
    skill_tools = [t for t in agent.tool_registry.list_tools() if isinstance(t, SkillTool)]
    assert any(t.name == "format_audit" for t in skill_tools)


@pytest.mark.asyncio
async def test_build_agent_works_without_domains():
    """No domain packages configured → still builds with an empty catalog."""
    reg = ToolRegistry()
    agent, _, _ = await build_agent(
        settings=_DummySettings(),
        tool_registry=reg,
        courtier_config=SimpleNamespace(domains=[]),
    )
    assert isinstance(agent, OrchestratorAgent)


@pytest.mark.asyncio
async def test_agents_built_back_to_back_do_not_share_artifact_store():
    """Regression: sessions must not share one ArtifactStore instance.

    Passing the app-global store into every session leaked artifacts (and
    therefore prior conversations' document content) into new sessions.
    """
    _, cm1, _ = await build_agent(settings=_DummySettings())
    _, cm2, _ = await build_agent(settings=_DummySettings())
    assert cm1._cache is not cm2._cache

    agent1, _, _ = await build_agent(settings=_DummySettings(), tool_registry=ToolRegistry())
    agent2, _, _ = await build_agent(settings=_DummySettings(), tool_registry=ToolRegistry())
    assert agent1._agent_runtime.artifact_store is not agent2._agent_runtime.artifact_store


def test_build_model_client_wraps_openai_backend():
    client = build_model_client(_DummySettings())

    assert isinstance(client, BackendModelClient)
    assert client.model_name == "mock"
    assert callable(client.generate_stream_full)


def test_build_model_client_uses_router_when_fallback_backends_configured():
    settings = _DummySettings()
    settings.agent_runtime = SimpleNamespace(
        model=SimpleNamespace(
            strategy="primary",
            fallback_backends=["backup-model"],
            cost_threshold_chars=None,
            ab_split=0.5,
        )
    )

    client = build_model_client(settings)

    assert isinstance(client, BackendModelClient)
    assert isinstance(client._backend, ModelRouter)
    assert len(client._backend._backends) == 2


def test_path_policy_seeding_tracks_self_declared_tools():
    """自声明 {"path": [...]} 的工具获得专属根；未声明工具回退老名单基线。"""
    from courtier.agent.core.capability import CapabilityRegistry
    from courtier.agent.core.guardrails import PathPolicyGuard
    from courtier.agent.core.guardrails.permission_guards import (
        declare_path_policy_tools,
    )

    class ExportTool:
        name = "export_report"
        path_policy = {"path": ["/srv/reports"]}

    class PlainTool:
        name = "plain"

    registry = CapabilityRegistry()
    declared = declare_path_policy_tools(
        registry, [ExportTool(), PlainTool()]
    )
    assert declared == ["export_report"]
    guard = PathPolicyGuard(allowed_roots=["/tmp"], capability_registry=registry)
    # 自声明工具：恰好声明的根
    assert guard._managed_roots("export_report") == ["/srv/reports"]
    # 未声明工具：不受 Capability 管（老名单基线由守卫自身的 _path_tools 兜底）
    assert guard._managed_roots("plain") is None
