"""Integration tests for AgentLoop.

TODO: Add streaming interruption scenario tests:
- Partial JSON from the model (incomplete tool call arguments)
- Connection drops mid-stream
- SSE client disconnection handling
These scenarios currently lack coverage and could hide recovery-path bugs.
"""

import pytest

from courtier.agent.core.model import ModelResponse, ToolCall
from courtier.agent.testing import MockModelClient
from courtier.agent.core.state import AgentState, Message
from courtier.agent.core.loop import agent_loop, _inject_reminder
from courtier.agent.core.loop_streaming import generate_with_streaming_fallback
from courtier.agent.core.execution_result import ExecutionResult
from courtier.agent.tools.registry import ToolRegistry
from courtier.common.behavioral_rules import PRE_TURN_REMINDER, PERIODIC_REMINDER


class TestInjectReminder:
    def test_appends_reminder_when_none_exists(self):
        msgs = (
            Message(role="system", content="System"),
            Message(role="user", content="Task"),
        )
        result = _inject_reminder(msgs, PRE_TURN_REMINDER)
        assert result[-1].role == "user"
        assert result[-1].content == PRE_TURN_REMINDER
        assert result[-1].source == "reminder"

    def test_removes_previous_same_type_reminder(self):
        """Pre-turn reminders must not accumulate across turns."""
        msgs = (
            Message(role="system", content="System"),
            Message(role="user", content="Task"),
            Message(role="user", content=PRE_TURN_REMINDER, source="reminder"),
            Message(role="assistant", content="Think"),
            Message(role="tool", content='{"data":"x"}', tool_call_id="t1", name="tool1"),
        )
        result = _inject_reminder(msgs, PRE_TURN_REMINDER)
        reminder_count = sum(
            1 for m in result
            if m.role == "user" and m.content == PRE_TURN_REMINDER
        )
        assert reminder_count == 1
        assert result[-1].content == PRE_TURN_REMINDER
        assert result[-1].source == "reminder"
        # Other messages must be preserved.
        assert any(m.role == "system" for m in result)
        assert any(m.content == "Task" for m in result)
        assert any(m.role == "assistant" for m in result)
        assert any(m.role == "tool" for m in result)

    def test_does_not_remove_other_reminder_type(self):
        """Injecting a periodic reminder should keep the pre-turn reminder."""
        msgs = (
            Message(role="system", content="System"),
            Message(role="user", content="Task"),
            Message(role="user", content=PRE_TURN_REMINDER, source="reminder"),
            Message(role="assistant", content="Think"),
        )
        result = _inject_reminder(msgs, PERIODIC_REMINDER)
        assert any(
            m.content == PRE_TURN_REMINDER and m.source == "reminder" for m in result
        )
        assert result[-1].content == PERIODIC_REMINDER
        assert result[-1].source == "reminder"

    def test_multiple_old_reminders_are_collapsed(self):
        """Several old reminders of the same type collapse to the latest one."""
        msgs = (
            Message(role="system", content="System"),
            Message(role="user", content=PRE_TURN_REMINDER, source="reminder"),
            Message(role="assistant", content="Think 1"),
            Message(role="user", content=PRE_TURN_REMINDER, source="reminder"),
            Message(role="assistant", content="Think 2"),
        )
        result = _inject_reminder(msgs, PRE_TURN_REMINDER)
        reminder_count = sum(
            1 for m in result
            if m.role == "user" and m.content == PRE_TURN_REMINDER
        )
        assert reminder_count == 1
        assert result[-1].content == PRE_TURN_REMINDER
        assert result[-1].source == "reminder"

    def test_preserves_user_message_with_same_content(self):
        """A genuine user message matching reminder text must not be removed."""
        msgs = (
            Message(role="system", content="System"),
            Message(role="user", content=PRE_TURN_REMINDER),
            Message(role="assistant", content="Think"),
        )
        result = _inject_reminder(msgs, PRE_TURN_REMINDER)
        assert any(
            m.content == PRE_TURN_REMINDER and m.source is None for m in result
        )
        assert result[-1].content == PRE_TURN_REMINDER
        assert result[-1].source == "reminder"


class TestAgentLoop:
    @pytest.mark.asyncio
    async def test_full_loop_think_act_observe(self, registry_with_echo):
        """End-to-end: model calls echo tool, tool returns result, loop completes."""
        tc = ToolCall(id="call_1", name="echo", arguments={"text": "hello world"})
        model = MockModelClient(tool_calls=[tc])

        state = AgentState.initial(task="echo hello world")
        final = await agent_loop(
            state=state, model=model, tool_registry=registry_with_echo
        )

        assert final.status == "completed"
        assert len(final.tool_results) == 1
        assert final.tool_results[0].raw_data == "hello world"

    @pytest.mark.asyncio
    async def test_multi_step_loop(self, registry_with_echo):
        """Model calls tool on step 1, returns text on step 2 — two iterations."""
        tc = ToolCall(id="1", name="echo", arguments={"text": "ping"})
        model = MockModelClient(tool_calls=[tc])

        state = AgentState.initial(task="echo ping")
        final = await agent_loop(
            state=state, model=model, tool_registry=registry_with_echo
        )

        assert final.status == "completed"
        assert len(final.tool_results) == 1
        assert final.tool_results[0].raw_data == "ping"
        # Second model call returned text
        assert final.messages[-1].content == "Done."

    @pytest.mark.asyncio
    async def test_loop_terminates_when_no_tool_calls(self):
        """When model returns content without tool calls, loop ends."""
        registry = ToolRegistry()
        model = MockModelClient(tool_calls=[])  # no tool calls

        state = AgentState.initial(task="say hi")
        final = await agent_loop(state=state, model=model, tool_registry=registry)

        assert final.status == "completed"
        assert final.messages[-1].content == "Done."

    @pytest.mark.asyncio
    async def test_max_steps_applies_to_repeated_tool_call_turns(self, registry_with_echo):
        """A model that keeps requesting tools must stop at max_steps."""

        class _AlwaysToolCallModel:
            model_name = "always-tool-call"
            temperature = 0.0

            async def generate(self, messages, tools=None, **kwargs):
                return ModelResponse(
                    content=None,
                    tool_calls=[ToolCall(id="call_1", name="echo", arguments={"text": "ping"})],
                )

        model = _AlwaysToolCallModel()
        state = AgentState.initial(task="echo forever", max_steps=2)
        final = await agent_loop(
            state=state,
            model=model,
            tool_registry=registry_with_echo,
        )

        assert final.status == "completed"
        assert final.termination_reason == "max_steps"
        assert final.current_step == 2

    @pytest.mark.asyncio
    async def test_loop_model_error_sets_error_state(self):
        """When model.generate raises, loop returns error state."""

        class _FailingModel:
            model_name = "failing-model"
            temperature = 0.0

            async def generate(self, messages, tools=None, **kwargs):
                raise RuntimeError("Connection refused")

        state = AgentState.initial(task="test")
        final = await agent_loop(state=state, model=_FailingModel(), tool_registry=None)

        assert final.status == "error"
        assert "Connection refused" in (final.termination_reason or "")

    @pytest.mark.asyncio
    async def test_loop_permissions_block_tool(self, registry_with_echo):
        """When permission gate denies a tool call, loop is blocked."""
        from courtier.agent.permissions.gate import PermissionGate

        tc = ToolCall(id="call_1", name="echo", arguments={"text": "secret"})
        model = MockModelClient(tool_calls=[tc])

        gate = PermissionGate()
        gate.block("echo")

        state = AgentState.initial(task="echo secret")
        final = await agent_loop(
            state=state, model=model, tool_registry=registry_with_echo, permissions=gate
        )

        assert final.status == "blocked"
        assert "Permission denied" in (final.termination_reason or "")

    @pytest.mark.asyncio
    async def test_tool_execution_error(self):
        """When a tool raises, the result is success=False."""

        class _FailingTool:
            name = "failer"
            description = "Always fails"
            parameters = {"type": "object", "properties": {}}

            async def execute(self, **kwargs):
                raise ValueError("Tool broke")

        registry = ToolRegistry()
        registry.register(_FailingTool())

        tc = ToolCall(id="1", name="failer", arguments={})
        model = MockModelClient(tool_calls=[tc])

        state = AgentState.initial(task="fail")
        final = await agent_loop(state=state, model=model, tool_registry=registry)

        assert final.status == "completed"
        assert len(final.tool_results) == 1
        assert final.tool_results[0].success is False
        assert "Tool broke" in final.tool_results[0].error


class TestGenerateWithStreamingFallback:
    """Tests for generate_with_streaming_fallback token streaming behavior."""

    @pytest.mark.asyncio
    async def test_streams_content_when_tool_calls_exist(self):
        """Content should be streamed via on_content_token even when tool calls are present."""
        tc = ToolCall(id="call_1", name="search", arguments={"query": "test"})
        model = MockModelClient(tool_calls=[tc])
        original_generate = model.generate

        async def _generate_with_content(messages, tools=None, **kwargs):
            resp = await original_generate(messages, tools=tools, **kwargs)
            return ModelResponse(
                content="Let me search for that document.",
                tool_calls=resp.tool_calls,
            )

        model.generate = _generate_with_content

        reasoning_tokens: list[str] = []
        content_tokens: list[str] = []

        async def _on_token(token: str) -> None:
            reasoning_tokens.append(token)

        async def _on_content_token(token: str) -> None:
            content_tokens.append(token)

        result, streamed = await generate_with_streaming_fallback(
            model=model,
            messages=[{"role": "user", "content": "test"}],
            tools=None,
            on_token=_on_token,
            on_content_token=_on_content_token,
        )

        assert len(content_tokens) > 0
        assert "".join(content_tokens) == "Let me search for that document."
        assert streamed is False
        assert len(result.tool_calls) == 1

    @pytest.mark.asyncio
    async def test_returns_true_when_content_without_tool_calls(self):
        """When no tool calls and content present, tokens are streamed via on_content_token."""
        model = MockModelClient(tool_calls=[])

        content_tokens: list[str] = []

        async def _on_token(token: str) -> None:
            pass

        async def _on_content_token(token: str) -> None:
            content_tokens.append(token)

        result, streamed = await generate_with_streaming_fallback(
            model=model,
            messages=[{"role": "user", "content": "say hi"}],
            tools=None,
            on_token=_on_token,
            on_content_token=_on_content_token,
        )

        assert len(content_tokens) > 0
        assert "Done." in "".join(content_tokens)
        assert streamed is True
        assert len(result.tool_calls) == 0

    @pytest.mark.asyncio
    async def test_no_token_calls_when_content_is_none(self):
        """When content is None, neither callback is called."""
        tc = ToolCall(id="call_1", name="search", arguments={"query": "test"})
        model = MockModelClient(tool_calls=[tc])

        reasoning_count = 0
        content_count = 0

        async def _on_token(token: str) -> None:
            nonlocal reasoning_count
            reasoning_count += 1

        async def _on_content_token(token: str) -> None:
            nonlocal content_count
            content_count += 1

        result, streamed = await generate_with_streaming_fallback(
            model=model,
            messages=[{"role": "user", "content": "find docs"}],
            tools=None,
            on_token=_on_token,
            on_content_token=_on_content_token,
        )

        assert reasoning_count == 0
        assert content_count == 0
        assert streamed is False
        assert len(result.tool_calls) == 1


class TestLoopRefResolution:
    @pytest.mark.asyncio
    async def test_context_manager_passed_to_execute(self):
        """agent_loop passes context_manager to tool_registry.execute."""
        from unittest.mock import AsyncMock, MagicMock

        tc = ToolCall(id="call_1", name="echo", arguments={"text": "hello"})
        model = MockModelClient(tool_calls=[tc])

        registry = ToolRegistry()
        mock_execute = AsyncMock(
            return_value=ExecutionResult(
                success=True, actor_type="tool", actor_name="echo", raw_data="echoed"
            )
        )
        registry.execute = mock_execute

        mock_cm = MagicMock()
        mock_cm.compact_if_needed = AsyncMock(return_value=[])
        mock_cm.micro_compact = AsyncMock(return_value=[])
        mock_cm.persist_large_output = MagicMock(return_value="persisted")
        mock_cm.resolve_refs = MagicMock(side_effect=lambda kwargs: kwargs)

        state = AgentState.initial(task="echo hello")
        await agent_loop(
            state=state,
            model=model,
            tool_registry=registry,
            context_manager=mock_cm,
        )

        mock_execute.assert_called_once()
        call_kwargs = mock_execute.call_args
        # Context manager is passed either as a keyword argument or the second
        # positional argument (after tool_name).
        cm_from_kwargs = call_kwargs.kwargs.get("context_manager")
        cm_from_args = (
            call_kwargs.args[1] if len(call_kwargs.args) > 1 else None
        )
        assert cm_from_kwargs is mock_cm or cm_from_args is mock_cm


@pytest.mark.asyncio
async def test_business_artifact_guard_terminates_after_no_progress():
    """After MAX_TURNS_WITHOUT_BUSINESS_ARTIFACTS turns without new business artifacts,
    the loop should terminate."""
    from unittest.mock import AsyncMock, MagicMock

    from courtier.agent.artifacts.store import ArtifactStore
    from courtier.agent.artifacts.models import ArtifactMetadata

    registry = ToolRegistry()
    mock_tool = MagicMock()
    mock_tool.name = "echo"
    mock_tool.description = "mock"
    mock_tool.parameters = {"type": "object", "properties": {}}
    mock_tool.execute = AsyncMock(
        return_value=ExecutionResult(
            success=True, actor_type="tool", actor_name="echo", raw_data="ok"
        )
    )
    registry.register(mock_tool)

    store = ArtifactStore()
    # Pre-populate with one business artifact
    from courtier.agent.artifacts.models import Artifact
    store.put(Artifact(
        artifact_id="business_1",
        artifact_type="core.plain_text",
        data={"text": "initial"},
        metadata=ArtifactMetadata(created_by="test", debug_only=False),
    ))

    tc = ToolCall(id="call_1", name="echo", arguments={})
    # Each turn: model calls echo → produces no new business artifacts
    model = MockModelClient(tool_calls=[tc])

    state = AgentState.initial(task="echo forever", max_steps=20)
    final = await agent_loop(
        state=state, model=model, tool_registry=registry,
        artifact_store=store,
    )

    # Should terminate — not due to max_steps (20) but due to no business progress
    assert final.status == "completed"
    # The echo tool doesn't create any new artifacts in the store,
    # so after 4 turns without business artifacts the loop should stop
    assert final.current_step < 20
