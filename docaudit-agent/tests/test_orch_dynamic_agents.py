"""Test OrchestratorAgent dynamic subagent discovery."""

from unittest.mock import MagicMock

import pytest


@pytest.mark.asyncio
async def test_orchestrator_ignores_plugin_agents():
    """Plugin-declared agents are no longer wrapped as subagents.

    The redesign migrated domain auditors from plugin ``type: agent``
    roles to Skills invoked through ``AgentRuntime``.  The orchestrator
    no longer builds subagents from ``plugin_system.get_agents()``; there
    are no built-in sub_ agents anymore.
    """
    from courtier.agent.agents.orch import OrchestratorAgent
    from courtier.agent.testing import MockModelClient

    # A plugin system that still reports agent roles must be ignored.
    mock_ps = MagicMock()

    model = MockModelClient(tool_calls=[])
    agent = OrchestratorAgent(
        model=model,
        plugin_system=mock_ps,
    )

    # Plugin agents are NOT turned into subagents.
    assert "format_auditor" not in agent.skill_names
    assert "content_auditor" not in agent.skill_names
    assert "transform" not in agent.skill_names
    assert agent.skill_names == []


@pytest.mark.asyncio
async def test_orchestrator_without_plugin_system_works():
    """OrchestratorAgent works without plugin_system (backward compat)."""
    from courtier.agent.agents.orch import OrchestratorAgent
    from courtier.agent.testing import MockModelClient

    model = MockModelClient(tool_calls=[])
    agent = OrchestratorAgent(model=model)  # No plugin_system
    assert agent.skill_names == []
