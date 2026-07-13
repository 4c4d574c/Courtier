"""Tests for Agent base class ref instruction injection."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from courtier.agent.agents.base import Agent


@pytest.fixture
def agent_with_cm():
    """An Agent with a mock context_manager."""
    agent = Agent(
        name="TestAgent",
        role="You are a test agent.",
        model=MagicMock(),
    )
    mock_cm = MagicMock()
    mock_cm.get_ref_instructions.return_value = (
        "Use $ref:tool:N as parameter value to reference persisted outputs."
    )
    return agent, mock_cm


def _make_fake_loop(captured: list):
    """Return an async fake agent_loop that records state into *captured*."""

    async def _fake_loop(**kwargs):
        from courtier.agent.core.state import AgentState

        captured.append(kwargs["state"])
        return AgentState(status="completed", messages=())

    return _fake_loop


class TestRefInstructionInjection:
    @pytest.mark.asyncio
    async def test_system_prompt_includes_ref_instructions(self, agent_with_cm):
        agent, mock_cm = agent_with_cm
        captured: list = []

        with patch(
            "courtier.agent.agents.base.agent_loop",
            _make_fake_loop(captured),
        ):
            await agent.run("test task", context_manager=mock_cm)

        assert len(captured) == 1
        system_msg = captured[0].messages[0]
        assert "$ref:" in system_msg.content

    @pytest.mark.asyncio
    async def test_no_injection_without_context_manager(self, agent_with_cm):
        agent, _ = agent_with_cm
        captured: list = []

        with patch(
            "courtier.agent.agents.base.agent_loop",
            _make_fake_loop(captured),
        ):
            await agent.run("test task")

        assert len(captured) == 1
        system_msg = captured[0].messages[0]
        assert "$ref:" not in system_msg.content


class TestBuiltinArtifactTools:
    @pytest.mark.asyncio
    async def test_registers_artifact_tools_when_context_manager_provided(
        self, agent_with_cm
    ):
        """Agents receiving a context_manager must expose the artifact
        introspection tools advertised by ContextManager.get_ref_instructions().
        """
        agent, mock_cm = agent_with_cm

        with patch(
            "courtier.agent.agents.base.agent_loop",
            _make_fake_loop([]),
        ):
            await agent.run("test task", context_manager=mock_cm)

        tool_names = {t.name for t in agent.tool_registry.list_tools()}
        assert "list_artifacts" in tool_names
        assert "get_artifact" in tool_names

    @pytest.mark.asyncio
    async def test_does_not_register_artifact_tools_without_context_manager(
        self, agent_with_cm
    ):
        agent, _ = agent_with_cm

        with patch(
            "courtier.agent.agents.base.agent_loop",
            _make_fake_loop([]),
        ):
            await agent.run("test task")

        tool_names = {t.name for t in agent.tool_registry.list_tools()}
        assert "list_artifacts" not in tool_names
        assert "get_artifact" not in tool_names
