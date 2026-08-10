"""Tests for agent_service wiring."""

from types import SimpleNamespace

import pytest

from courtier.agent.agents.orch import OrchestratorAgent
from courtier.agent.api.services.agent_service import build_audit_agent, build_model_client
from courtier.agent.core.backends.router import ModelRouter
from courtier.agent.core.model import BackendModelClient
from courtier.agent.runtime import AgentRuntime
from courtier.agent.tools.registry import ToolRegistry


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


@pytest.mark.asyncio
async def test_build_audit_agent_creates_orchestrator_with_runtime():
    reg = ToolRegistry()
    agent, context_manager, model_name = await build_audit_agent(
        settings=_DummySettings(),
        tool_registry=reg,
    )

    assert isinstance(agent, OrchestratorAgent)
    assert isinstance(agent._agent_runtime, AgentRuntime)

    # GetArtifactTool is registered by OrchestratorAgent, not agent_service.
    # Verify the orchestrator has the artifact tools available.
    orchestrator_tools = {t.name for t in agent.tool_registry.list_tools()}
    assert "get_artifact" in orchestrator_tools
    assert "list_artifacts" in orchestrator_tools


@pytest.mark.asyncio
async def test_build_audit_agent_uses_disk_result_store_when_es_not_configured():
    reg = ToolRegistry()
    agent, _, _ = await build_audit_agent(
        settings=_DummySettings(),
        tool_registry=reg,
    )

    assert agent._agent_runtime.artifact_store is not None


@pytest.mark.asyncio
async def test_build_audit_agent_requires_configured_domain(monkeypatch):
    """When no domain packages are configured, build_audit_agent raises."""
    monkeypatch.setenv("COURTIER_DOMAIN_PACKAGES", "")
    monkeypatch.setenv("COURTIER_REPO_ROOT", "/tmp")

    with pytest.raises(ValueError, match="No domain packages configured"):
        await build_audit_agent(
            settings=_DummySettings(),
            tool_registry=ToolRegistry(),
        )


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


# ---- build_chat_agent shared-plugin tool wiring ------------------------------


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


@pytest.mark.asyncio
async def test_build_chat_agent_registers_only_shared_plugin_tools():
    from courtier.agent.api.services.agent_service import build_chat_agent

    reg = ToolRegistry()
    reg.register(_fake_plugin_tool("search_documents", "search"))
    reg.register(_fake_plugin_tool("parse_document", "parse"))
    reg.register(_fake_plugin_tool("format_audit", "format_audit"))

    agent, _, _ = await build_chat_agent(
        settings=_DummySettings(),
        tool_registry=reg,
        shared_plugin_names={"search", "parse", "annotate", "template"},
    )

    chat_tools = {t.name for t in agent.tool_registry.list_tools()}
    assert "search_documents" in chat_tools
    assert "parse_document" in chat_tools
    assert "format_audit" not in chat_tools  # domain audit plugin excluded


@pytest.mark.asyncio
async def test_build_chat_agent_without_registry_has_no_plugin_tools():
    from courtier.agent.api.services.agent_service import build_chat_agent

    agent, _, _ = await build_chat_agent(settings=_DummySettings())

    chat_tools = {t.name for t in agent.tool_registry.list_tools()}
    # Only the builtin artifact tools auto-registered via context_manager.
    assert "search_documents" not in chat_tools


@pytest.mark.asyncio
async def test_agents_built_back_to_back_do_not_share_artifact_store():
    """Regression: sessions must not share one ArtifactStore instance.

    Passing the app-global store into every session leaked artifacts (and
    therefore prior conversations' document content) into new sessions.
    """
    from courtier.agent.api.services.agent_service import (
        build_audit_agent,
        build_chat_agent,
    )

    _, cm1, _ = await build_chat_agent(settings=_DummySettings())
    _, cm2, _ = await build_chat_agent(settings=_DummySettings())
    assert cm1._cache is not cm2._cache

    agent1, _, _ = await build_audit_agent(settings=_DummySettings(), tool_registry=ToolRegistry())
    agent2, _, _ = await build_audit_agent(settings=_DummySettings(), tool_registry=ToolRegistry())
    assert agent1._agent_runtime.artifact_store is not agent2._agent_runtime.artifact_store


# ---- chat registry result-handling (summarizer / persist parity) --------------


@pytest.mark.asyncio
async def test_build_chat_agent_configures_result_summarizer():
    """Chat mode must summarise+persist large tool results like audit mode,
    instead of dumping full text into the LLM context."""
    from courtier.agent.api.services.agent_service import build_chat_agent

    agent, cm, _ = await build_chat_agent(settings=_DummySettings())

    summarizer = getattr(agent.tool_registry, "_summarizer", None)
    assert summarizer is not None
    # Persists into the same store the context manager resolves refs from.
    assert summarizer.artifact_store is cm._cache


@pytest.mark.asyncio
async def test_build_chat_agent_large_tool_result_gets_ref():
    from types import SimpleNamespace as _SN

    from courtier.agent.api.services.agent_service import build_chat_agent
    from courtier.agent.tools.protocol import ToolResult

    class _BigTool:
        name = "big_reader"
        description = ""
        parameters = {"type": "object", "properties": {}}
        runtime_policy = None
        skip_persist = False
        _client = _SN(plugin_name="parse")

        async def execute(self, **kwargs):
            return ToolResult(success=True, data={"text": "x" * 4000})

    reg = ToolRegistry()
    reg.register(_BigTool())
    agent, cm, _ = await build_chat_agent(
        settings=_DummySettings(),
        tool_registry=reg,
        shared_plugin_names={"parse"},
    )

    result = await agent.tool_registry.execute("big_reader", artifact_store=cm._cache)

    assert result.success is True
    assert result.raw_data is None  # full text no longer enters the context
    assert result.result_id is not None
    assert cm._cache.load(result.result_id) == {"text": "x" * 4000}


# ---- plugin system_prompt injection wiring ------------------------------------


class _FakePluginSystem:
    def __init__(self, prompts: dict[str, str]):
        self._prompts = prompts

    def get_system_prompts(self) -> dict[str, str]:
        return dict(self._prompts)


@pytest.mark.asyncio
async def test_build_audit_agent_injects_plugin_system_prompts():
    reg = ToolRegistry()
    reg.register(_fake_plugin_tool("parse_document", "parse"))

    agent, _, _ = await build_audit_agent(
        settings=_DummySettings(),
        tool_registry=reg,
        plugin_system=_FakePluginSystem({"parse": "parse 插件用法：先解析再审核"}),
    )

    prompt = agent.build_system_prompt()
    assert "parse 插件用法：先解析再审核" in prompt


@pytest.mark.asyncio
async def test_build_chat_agent_injects_shared_plugin_system_prompts():
    from courtier.agent.api.services.agent_service import build_chat_agent

    reg = ToolRegistry()
    reg.register(_fake_plugin_tool("parse_document", "parse"))

    agent, _, _ = await build_chat_agent(
        settings=_DummySettings(),
        tool_registry=reg,
        shared_plugin_names={"parse"},
        plugin_system=_FakePluginSystem(
            {"parse": "parse 插件用法说明", "format_audit": "不应出现在 chat"}
        ),
    )

    prompt = agent.build_system_prompt()
    assert "parse 插件用法说明" in prompt
    assert "不应出现在 chat" not in prompt  # domain plugin filtered out
