"""Tests for Agent base class."""

import pytest

from courtier.agent.agents.base import Agent, AgentResult
from courtier.agent.core.model import ToolCall
from courtier.agent.testing import MockModelClient


class TestAgent:
    @pytest.mark.asyncio
    async def test_run_completes(self, echo_tool):
        tc = ToolCall(id="1", name="echo", arguments={"text": "hello"})
        model = MockModelClient(tool_calls=[tc])

        agent = Agent(
            name="EchoAgent",
            role="Echo back text using the echo tool.",
            tools=[echo_tool],
            model=model,
        )
        result = await agent.run("echo hello")
        assert result.status == "completed"
        assert len(result.tool_results) == 1

    @pytest.mark.asyncio
    async def test_agent_result_from_state(self):
        from courtier.agent.core.state import AgentState

        state = AgentState.initial(task="test")
        final = state.model_copy(
            update={
                "status": "completed",
                "termination_reason": "completed",
            }
        )
        result = AgentResult.from_state(final)
        assert result.status == "completed"
        assert result.termination_reason == "completed"

    def test_system_prompt_includes_role_and_tools(self, echo_tool):
        model = MockModelClient()
        agent = Agent(
            name="TestAgent",
            role="You are a test agent.",
            tools=[echo_tool],
            model=model,
        )
        prompt = agent.build_system_prompt()
        assert "You are a test agent." in prompt
        assert "echo" in prompt
        # Tool names appear in compact list; descriptions now only in API tools param
        assert "可用工具" in prompt

    def test_system_prompt_includes_behavioral_rules(self, echo_tool):
        model = MockModelClient()
        agent = Agent(
            name="TestAgent",
            role="You are a test agent.",
            tools=[echo_tool],
            model=model,
        )
        prompt = agent.build_system_prompt()
        assert "禁止在最终输出中暴露任何内部实现细节" in prompt
        assert "Markdown 格式" in prompt
        assert "关键路径工具" in prompt
        assert "终止信号" in prompt
