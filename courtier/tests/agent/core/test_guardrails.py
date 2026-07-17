"""Tests for GuardrailSystem."""

import pytest

from courtier.agent.core.guardrails import (
    BusinessArtifactProgressGuard,
    DangerousToolGuard,
    EmptyOutputGuard,
    ExploreLoopGuard,
    GuardContext,
    GuardrailSystem,
    GuardResult,
    RefusalOutputGuard,
    SensitiveInputGuard,
)
from courtier.agent.core.state import AgentState, Message
from courtier.agent.core.tool_call import ToolCall
from courtier.agent.testing import MockModelClient
from courtier.agent.tools.builtin.echo import EchoTool
from courtier.agent.tools.registry import ToolRegistry


class _BlockingInputGuard:
    name = "block_input"
    layer = "input"

    async def check(self, context: GuardContext) -> GuardResult:
        return GuardResult.block(self.name, "input blocked")


class _LoggingOutputGuard:
    name = "log_output"
    layer = "output"

    async def check(self, context: GuardContext) -> GuardResult:
        return GuardResult.log(self.name, "output logged")


class TestGuardrailSystem:
    @pytest.mark.asyncio
    async def test_allow_by_default(self):
        system = GuardrailSystem()
        result = await system.check("input", GuardContext())
        assert result.action == "allow"

    @pytest.mark.asyncio
    async def test_input_block(self):
        system = GuardrailSystem()
        system.register(_BlockingInputGuard())
        result = await system.check("input", GuardContext())
        assert result.action == "block"
        assert result.layer == "input"

    @pytest.mark.asyncio
    async def test_log_mode_downgrades_block(self):
        system = GuardrailSystem(input_mode="log")
        system.register(_BlockingInputGuard())
        result = await system.check("input", GuardContext())
        assert result.action == "log"
        assert result.metadata.get("downgraded_from") == "block"

    @pytest.mark.asyncio
    async def test_off_layer_skips_checks(self):
        system = GuardrailSystem(input_mode="off")
        system.register(_BlockingInputGuard())
        result = await system.check("input", GuardContext())
        assert result.action == "allow"

    @pytest.mark.asyncio
    async def test_event_handler_called(self):
        events = []

        async def handler(result: GuardResult) -> None:
            events.append(result)

        system = GuardrailSystem(on_event=handler)
        system.register(_LoggingOutputGuard())
        await system.check("output", GuardContext())
        assert len(events) == 1
        assert events[0].guard_name == "log_output"


class TestSensitiveInputGuard:
    @pytest.mark.asyncio
    async def test_detects_credit_card(self):
        guard = SensitiveInputGuard()
        result = await guard.check(
            GuardContext(messages=[Message(role="user", content="My card is 1234 5678 9012 3456")])
        )
        assert result.action == "log"

    @pytest.mark.asyncio
    async def test_allows_clean_input(self):
        guard = SensitiveInputGuard()
        result = await guard.check(
            GuardContext(messages=[Message(role="user", content="Hello")])
        )
        assert result.action == "allow"


class TestOutputGuards:
    @pytest.mark.asyncio
    async def test_empty_output(self):
        guard = EmptyOutputGuard()
        result = await guard.check(GuardContext(response_text="   "))
        assert result.action == "log"

    @pytest.mark.asyncio
    async def test_refusal_output(self):
        guard = RefusalOutputGuard()
        result = await guard.check(GuardContext(response_text="I'm sorry, I cannot do that."))
        assert result.action == "log"


class TestToolGuards:
    @pytest.mark.asyncio
    async def test_dangerous_tool_blocked(self):
        guard = DangerousToolGuard()
        result = await guard.check(
            GuardContext(tool_calls=[ToolCall(id="1", name="rm", arguments={})])
        )
        assert result.action == "block"

    @pytest.mark.asyncio
    async def test_safe_tool_allowed(self):
        guard = DangerousToolGuard()
        result = await guard.check(
            GuardContext(tool_calls=[ToolCall(id="1", name="echo", arguments={})])
        )
        assert result.action == "allow"


class TestGuardrailsInAgentLoop:
    @pytest.mark.asyncio
    async def test_input_guard_blocks_loop(self):
        from courtier.agent.core.loop import agent_loop

        state = AgentState.initial("task")
        system = GuardrailSystem()
        system.register(_BlockingInputGuard())

        final = await agent_loop(
            state=state,
            model=MockModelClient(tool_calls=[]),
            guardrail_system=system,
        )
        assert final.status == "blocked"
        assert "guardrail:block_input" in (final.termination_reason or "")

    @pytest.mark.asyncio
    async def test_tool_guard_blocks_tool_call(self):
        from courtier.agent.core.loop import agent_loop

        reg = ToolRegistry()
        reg.register(EchoTool())

        state = AgentState.initial("say hi")
        system = GuardrailSystem()
        system.register(DangerousToolGuard(dangerous_names={"echo"}))

        final = await agent_loop(
            state=state,
            model=MockModelClient(
                tool_calls=[ToolCall(id="1", name="echo", arguments={"text": "hi"})]
            ),
            tool_registry=reg,
            guardrail_system=system,
        )
        assert final.status == "blocked"


class _NullResultTool:
    name = "null_tool"
    description = "returns null"
    parameters = {"type": "object", "properties": {}}

    async def execute(self, **kwargs):
        from courtier.agent.core.execution_result import ExecutionResult
        return ExecutionResult(
            success=True, actor_type="tool", actor_name="null_tool", raw_data=None
        )


class TestLoopGuardrails:
    @pytest.mark.asyncio
    async def test_explore_loop_guard_blocks_null_results(self):
        guard = ExploreLoopGuard()
        # Vary arguments so the repeated-call guard does not trigger first.
        for i in range(4):
            ctx = GuardContext(
                tool_calls=[ToolCall(id=str(i), name="get_artifact", arguments={"id": i})],
                tool_results=[_null_result()],
            )
            result = await guard.check(ctx)
            assert result.action == "allow"

        ctx = GuardContext(
            tool_calls=[ToolCall(id="5", name="get_artifact", arguments={"id": 5})],
            tool_results=[_null_result()],
        )
        result = await guard.check(ctx)
        assert result.action == "block"
        assert "null" in result.reason

    @pytest.mark.asyncio
    async def test_explore_loop_guard_blocks_repeated_calls(self):
        guard = ExploreLoopGuard()
        tc = ToolCall(id="1", name="echo", arguments={"text": "hi"})
        ctx = GuardContext(tool_calls=[tc], tool_results=[_ok_result()])

        for _ in range(2):
            result = await guard.check(ctx)
            assert result.action == "allow"

        result = await guard.check(ctx)
        assert result.action == "block"
        assert "repeated" in result.reason

    @pytest.mark.asyncio
    async def test_explore_loop_guard_blocks_consecutive_exploratory(self):
        guard = ExploreLoopGuard()
        # Vary arguments so the repeated-call guard does not trigger first.
        for i in range(7):
            ctx = GuardContext(
                tool_calls=[ToolCall(id=str(i), name="get_artifact", arguments={"id": i})],
                tool_results=[_ok_result()],
            )
            result = await guard.check(ctx)
            assert result.action == "allow"

        ctx = GuardContext(
            tool_calls=[ToolCall(id="8", name="get_artifact", arguments={"id": 8})],
            tool_results=[_ok_result()],
        )
        result = await guard.check(ctx)
        assert result.action == "block"
        assert "exploratory" in result.reason

    @pytest.mark.asyncio
    async def test_business_artifact_guard_blocks_no_progress(self, monkeypatch):
        from unittest.mock import MagicMock

        from courtier.config import get_settings

        monkeypatch.setattr(
            get_settings(), "loop_max_turns_without_business_artifacts", 4
        )

        store = MagicMock()
        store.list_all.return_value = []

        guard = BusinessArtifactProgressGuard()
        ctx = GuardContext(metadata={"artifact_store": store})

        for _ in range(3):
            result = await guard.check(ctx)
            assert result.action == "allow"

        result = await guard.check(ctx)
        assert result.action == "block"
        assert "business artifacts" in result.reason

    @pytest.mark.asyncio
    async def test_business_artifact_guard_allows_progress(self):
        from unittest.mock import MagicMock

        class _Artifact:
            metadata = MagicMock(debug_only=False)

        store = MagicMock()
        store.list_all.side_effect = [[], [_Artifact()], [_Artifact()]]

        guard = BusinessArtifactProgressGuard()
        ctx = GuardContext(metadata={"artifact_store": store})

        result = await guard.check(ctx)
        assert result.action == "allow"
        result = await guard.check(ctx)
        assert result.action == "allow"

    @pytest.mark.asyncio
    async def test_explore_loop_guard_in_agent_loop(self):
        from courtier.agent.core.loop import agent_loop
        from courtier.agent.core.model import ModelResponse, ToolCall

        class _AlwaysExploreModel:
            model_name = "explore"
            temperature = 0.0

            async def generate(self, messages, tools=None, **kwargs):
                return ModelResponse(
                    content=None,
                    tool_calls=[ToolCall(id="1", name="get_artifact", arguments={})],
                )

        reg = ToolRegistry()
        reg.register(_NullResultTool())

        state = AgentState.initial("explore", max_steps=20)
        final = await agent_loop(
            state=state,
            model=_AlwaysExploreModel(),
            tool_registry=reg,
        )

        assert final.status == "completed"
        assert "explore_loop" in (final.termination_reason or "")
        assert final.current_step < 20


def _null_result():
    from courtier.agent.core.execution_result import ExecutionResult
    return ExecutionResult(success=True, actor_type="tool", actor_name="x", raw_data=None)


def _ok_result():
    from courtier.agent.core.execution_result import ExecutionResult
    return ExecutionResult(success=True, actor_type="tool", actor_name="x", raw_data="ok")
