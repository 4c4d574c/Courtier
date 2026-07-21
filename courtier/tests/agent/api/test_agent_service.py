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
