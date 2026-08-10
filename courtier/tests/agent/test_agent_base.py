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

    @pytest.mark.asyncio
    async def test_reminders_come_from_prompt_engine(self, echo_tool):
        """传入 PromptEngine 时，loop 的两个提醒来自引擎渲染结果。"""
        from courtier.prompts.engine import PromptBundle, PromptEngine

        engine = PromptEngine(
            PromptBundle(
                locale="zh-CN",
                templates={
                    "behavioral.thinking_directive": "# 思考",
                    "behavioral.rules": "# 规则",
                    "tools.invocation_rules": "# 工具规则",
                    "behavioral.pre_turn_reminder": "【引擎定制提醒】",
                    "behavioral.periodic_reminder": "【引擎阶段提醒】",
                },
            )
        )
        agent = Agent(
            name="EngineAgent",
            role="test",
            tools=[echo_tool],
            model=MockModelClient(tool_calls=[]),
            prompt_engine=engine,
        )
        assert agent._periodic_reminder == "【引擎阶段提醒】"

        result = await agent.run("测试任务")
        assert any(
            m.content == "【引擎定制提醒】" and m.source == "reminder"
            for m in result.final_state.messages
        )

    def test_reminders_fallback_without_engine(self, echo_tool):
        """无 PromptEngine 时保持硬编码常量（测试兼容路径）。"""
        from courtier.common.behavioral_rules import (
            PERIODIC_REMINDER,
            PRE_TURN_REMINDER,
        )

        agent = Agent(name="A", role="r", tools=[echo_tool], model=MockModelClient())
        assert agent._pre_turn_reminder == PRE_TURN_REMINDER
        assert agent._periodic_reminder == PERIODIC_REMINDER

    @pytest.mark.asyncio
    async def test_run_with_initial_state_does_not_duplicate_task(self, echo_tool):
        """Sub-agent runs pass AgentState.initial(task=...) — the task user
        message is already present and must not be appended a second time."""
        from courtier.agent.core.state import AgentState

        agent = Agent(
            name="SubAgent",
            role="You are a sub agent.",
            tools=[echo_tool],
            model=MockModelClient(tool_calls=[]),
        )
        state = AgentState.initial(task="do the audit", system_prompt="sp")
        result = await agent.run(task="do the audit", state=state)

        task_messages = [
            m
            for m in result.final_state.messages
            if m.role == "user" and m.content == "do the audit"
        ]
        assert len(task_messages) == 1

    @pytest.mark.asyncio
    async def test_run_with_history_state_appends_new_task(self, echo_tool):
        """Multi-turn continuation: history state + a new task still appends."""
        from courtier.agent.core.state import AgentState, Message

        agent = Agent(
            name="MainAgent",
            role="You are the main agent.",
            tools=[echo_tool],
            model=MockModelClient(tool_calls=[]),
        )
        state = AgentState.initial(task="old task", system_prompt="sp")
        state = state.model_copy(
            update={
                "messages": state.messages + (Message(role="assistant", content="old reply"),),
            }
        )
        result = await agent.run(task="new task", state=state)

        # The loop may inject its own user-role constraint messages; check
        # only the task messages' order and multiplicity.
        task_contents = [
            m.content
            for m in result.final_state.messages
            if m.role == "user" and m.content in ("old task", "new task")
        ]
        assert task_contents == ["old task", "new task"]
