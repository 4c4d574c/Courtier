"""Tests for EchoTool and EchoAgent."""

import pytest

from courtier.agent.agents.echo import create_echo_agent
from courtier.agent.tools.builtin.echo import EchoTool


class TestEchoTool:
    @pytest.mark.asyncio
    async def test_echo_returns_same_text(self):
        tool = EchoTool()
        result = await tool.execute(on_progress=lambda p: None, text="hello world")
        assert result.success
        assert result.data == "hello world"

    @pytest.mark.asyncio
    async def test_echo_empty_text(self):
        tool = EchoTool()
        result = await tool.execute(on_progress=lambda p: None, text="")
        assert result.success
        assert result.data == ""

    @pytest.mark.asyncio
    async def test_echo_with_extra_kwargs(self):
        tool = EchoTool()
        result = await tool.execute(on_progress=lambda p: None, text="hi", extra="ignored")
        assert result.success
        assert result.data == "hi"

    def test_tool_schema(self):
        tool = EchoTool()
        assert tool.name == "echo"
        assert "text" in tool.parameters.get("required", [])
        assert "properties" in tool.parameters


class TestEchoAgent:
    @pytest.mark.asyncio
    async def test_echo_agent_runs_and_echos(self):
        agent = create_echo_agent()
        result = await agent.run("hello world")
        assert result.status == "completed"
        assert len(result.tool_results) == 1
        assert result.tool_results[0].raw_data == "hello world"
