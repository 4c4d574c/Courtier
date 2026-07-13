"""Shared fixtures for agent tests."""

from __future__ import annotations

import pytest

from courtier.agent.core.state import AgentState
from courtier.agent.tools.builtin.echo import EchoTool
from courtier.agent.tools.protocol import ToolResult
from courtier.agent.tools.registry import ToolRegistry
from courtier.agent.agents.echo import create_echo_agent


@pytest.fixture(autouse=True)
def _clean_logging():
    """Reset logging handlers after each test to prevent leakage."""
    yield
    from courtier.agent.core.logging_config import reset_logging
    reset_logging()


@pytest.fixture
def echo_tool() -> EchoTool:
    """A real EchoTool instance."""
    return EchoTool()


@pytest.fixture
def fake_tool():
    """A generic fake tool for registry testing."""

    class _FakeTool:
        name: str = "fake"
        description: str = "A fake tool for testing"
        parameters: dict = {"type": "object", "properties": {}}

        async def execute(self, **kwargs):
            return ToolResult(success=True, data=kwargs.get("value", "default"))

    return _FakeTool()


@pytest.fixture
def echo_agent():
    """An EchoAgent built via the standard factory."""
    return create_echo_agent()


@pytest.fixture
def idle_state():
    """An idle AgentState ready for testing."""
    return AgentState.initial(task="test task")


@pytest.fixture
def empty_registry():
    """An empty ToolRegistry."""
    return ToolRegistry()


@pytest.fixture
def registry_with_echo(echo_tool):
    """A ToolRegistry with EchoTool registered."""
    reg = ToolRegistry()
    reg.register(echo_tool)
    return reg


@pytest.fixture
def registry_with_fake(fake_tool):
    """A ToolRegistry with _FakeTool registered."""
    reg = ToolRegistry()
    reg.register(fake_tool)
    return reg
