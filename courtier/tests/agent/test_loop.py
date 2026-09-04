"""Integration tests for AgentLoop.

TODO: Add streaming interruption scenario tests:
- Partial JSON from the model (incomplete tool call arguments)
- Connection drops mid-stream
- SSE client disconnection handling
These scenarios currently lack coverage and could hide recovery-path bugs.
"""

import pytest

from courtier.agent.core.execution_result import ExecutionResult
from courtier.agent.core.loop import _inject_reminder, agent_loop
from courtier.agent.core.loop_streaming import generate_with_streaming_fallback
from courtier.agent.core.model import ModelResponse, ToolCall
from courtier.agent.core.state import AgentState, Message
from courtier.agent.testing import MockModelClient
from courtier.agent.tools.registry import ToolRegistry
from courtier.prompts.engine import PromptEngine

# Reminder texts rendered from the core default bundles — the single source
# of truth (the old hardcoded constants in common/behavioral_rules.py were
# removed in favour of these YAML templates).
_ENGINE = PromptEngine.from_domain_directories([], locale="zh-CN")
PRE_TURN_REMINDER = _ENGINE.render("behavioral.pre_turn_reminder")
PERIODIC_REMINDER = _ENGINE.render("behavioral.periodic_reminder")


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
            1 for m in result if m.role == "user" and m.content == PRE_TURN_REMINDER
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
        assert any(m.content == PRE_TURN_REMINDER and m.source == "reminder" for m in result)
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
            1 for m in result if m.role == "user" and m.content == PRE_TURN_REMINDER
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
        assert any(m.content == PRE_TURN_REMINDER and m.source is None for m in result)
        assert result[-1].content == PRE_TURN_REMINDER
        assert result[-1].source == "reminder"


class TestLoopReminderParams:
    """agent_loop 的提醒文本参数化（生产路径由 PromptEngine 渲染传入）。"""

    @pytest.mark.asyncio
    async def test_custom_pre_turn_reminder_injected(self):
        state = AgentState.initial(task="测试任务", system_prompt="sys")
        final = await agent_loop(
            state=state,
            model=MockModelClient(tool_calls=[]),
            tool_registry=None,
            pre_turn_reminder="【自定义本轮约束】",
        )
        assert any(
            m.content == "【自定义本轮约束】" and m.source == "reminder" for m in final.messages
        )
        # 硬编码默认文本不再注入
        assert not any(m.content == PRE_TURN_REMINDER for m in final.messages)

    @pytest.mark.asyncio
    async def test_empty_pre_turn_reminder_disables_injection(self):
        state = AgentState.initial(task="测试任务", system_prompt="sys")
        final = await agent_loop(
            state=state,
            model=MockModelClient(tool_calls=[]),
            tool_registry=None,
            pre_turn_reminder="",
        )
        assert not any(m.source == "reminder" for m in final.messages)

    @pytest.mark.asyncio
    async def test_no_reminder_injected_by_default(self):
        """不传提醒参数时默认禁用注入 —— 提醒文本由上层（Agent/PromptEngine）
        渲染后显式传入，loop 本身不再内置硬编码默认文本。"""
        state = AgentState.initial(task="测试任务", system_prompt="sys")
        final = await agent_loop(
            state=state,
            model=MockModelClient(tool_calls=[]),
            tool_registry=None,
        )
        assert not any(m.source == "reminder" for m in final.messages)


class TestAgentLoop:
    @pytest.mark.asyncio
    async def test_full_loop_think_act_observe(self, registry_with_echo):
        """End-to-end: model calls echo tool, tool returns result, loop completes."""
        tc = ToolCall(id="call_1", name="echo", arguments={"text": "hello world"})
        model = MockModelClient(tool_calls=[tc])

        state = AgentState.initial(task="echo hello world")
        final = await agent_loop(state=state, model=model, tool_registry=registry_with_echo)

        assert final.status == "completed"
        assert len(final.tool_results) == 1
        assert final.tool_results[0].raw_data == "hello world"

    @pytest.mark.asyncio
    async def test_multi_step_loop(self, registry_with_echo):
        """Model calls tool on step 1, returns text on step 2 — two iterations."""
        tc = ToolCall(id="1", name="echo", arguments={"text": "ping"})
        model = MockModelClient(tool_calls=[tc])

        state = AgentState.initial(task="echo ping")
        final = await agent_loop(state=state, model=model, tool_registry=registry_with_echo)

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

            async def generate_stream_full(
                self, messages, tools=None, on_token=None, on_content_token=None, **kwargs
            ):
                return await self.generate(messages, tools=tools, **kwargs)

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

            async def generate_stream_full(
                self, messages, tools=None, on_token=None, on_content_token=None, **kwargs
            ):
                return await self.generate(messages, tools=tools, **kwargs)

        state = AgentState.initial(task="test")
        final = await agent_loop(state=state, model=_FailingModel(), tool_registry=None)

        assert final.status == "error"
        assert "Connection refused" in (final.termination_reason or "")

    @pytest.mark.asyncio
    async def test_loop_permissions_denial_is_single_call(self, registry_with_echo):
        """名级拒绝 = 单调用拒绝：被拒调用得到错误结果，run 继续完成。"""
        from courtier.agent.core.guardrails import GuardrailSystem, ToolDisabledGuard

        tc = ToolCall(id="call_1", name="echo", arguments={"text": "secret"})
        model = MockModelClient(tool_calls=[tc])

        system = GuardrailSystem()
        system.register(ToolDisabledGuard(blocked=("echo",)))

        state = AgentState.initial(task="echo secret")
        final = await agent_loop(
            state=state,
            model=model,
            tool_registry=registry_with_echo,
            guardrail_system=system,
        )

        assert final.status == "completed"  # not blocked — the run continues
        tool_msgs = [m for m in final.messages if m.role == "tool"]
        assert tool_msgs, "denied call must surface as a tool observation"
        assert any("已被禁用" in (m.content or "") for m in tool_msgs)

    async def test_loop_path_policy_denial_is_single_call(self, tmp_path):
        """参数级拒绝：read 越界路径 → 该调用被拒，run 继续完成。"""
        from courtier.agent.core.guardrails import GuardrailSystem, PathPolicyGuard
        from courtier.agent.tools.builtin.file_tools import ReadTool

        registry = ToolRegistry()
        registry.register(ReadTool())

        outside = tmp_path.parent / "outside-secret.txt"
        tc = ToolCall(id="call_1", name="read", arguments={"path": str(outside)})
        model = MockModelClient(tool_calls=[tc])

        system = GuardrailSystem()
        system.register(PathPolicyGuard(allowed_roots=[tmp_path / "memory"]))

        state = AgentState.initial(task="read the file")
        final = await agent_loop(
            state=state,
            model=model,
            tool_registry=registry,
            guardrail_system=system,
        )

        assert final.status == "completed"
        tool_msgs = [m for m in final.messages if m.role == "tool"]
        assert any("不在允许范围内" in (m.content or "") for m in tool_msgs)

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_session_system_gets_loop_guards_and_permissions(self, tmp_path):
        """Provided 会话系统：权限 guard 生效 + loop 护栏运行期注册/结束释放。"""
        from courtier.agent.core.guardrails import GuardrailSystem, PathPolicyGuard
        from courtier.agent.tools.builtin.file_tools import ReadTool

        registry = ToolRegistry()
        registry.register(ReadTool())

        outside = tmp_path.parent / "outside-secret.txt"
        tc = ToolCall(id="call_1", name="read", arguments={"path": str(outside)})
        model = MockModelClient(tool_calls=[tc])

        system = GuardrailSystem()
        system.register(PathPolicyGuard(allowed_roots=[tmp_path / "memory"]))
        names_before = [g.name for g in system.guardrails]

        state = AgentState.initial(task="read the file")
        final = await agent_loop(
            state=state,
            model=model,
            tool_registry=registry,
            guardrail_system=system,
        )

        assert final.status == "completed"
        tool_msgs = [m for m in final.messages if m.role == "tool"]
        assert any("不在允许范围内" in (m.content or "") for m in tool_msgs)
        # Loop guards were registered for the run and released afterwards.
        assert [g.name for g in system.guardrails] == names_before

    @pytest.mark.asyncio
    async def test_post_tool_observer_dispatched_in_real_loop(self, registry_with_echo):
        """run_scope 接线：注册在 post_tool 的观察者随真实循环派发。

        循环曾直连 check() 绕过 run_scope，观察者注册后静默无效——本测试
        锁定派发路径（guardrails-wiring-cleanup-plan T1）。
        """
        from courtier.agent.core.guardrails import GuardrailSystem

        seen: list = []

        async def observer(context):
            seen.append(context)

        tc = ToolCall(id="call_1", name="echo", arguments={"text": "hi"})
        system = GuardrailSystem()
        system.register_observer("post_tool", observer)

        state = AgentState.initial(task="echo hi")
        final = await agent_loop(
            state=state,
            model=MockModelClient(tool_calls=[tc]),
            tool_registry=registry_with_echo,
            guardrail_system=system,
            agent_name="Tester",
        )

        assert final.status == "completed"
        assert seen, "post_tool observer never dispatched — run_scope not wired"
        assert seen[0].tool_calls
        # 全链：循环入口 set_context → run_scope 富化 → 观察者读到身份。
        assert seen[0].agent_name == "Tester"

    @pytest.mark.asyncio
    async def test_pre_think_interceptor_state_swap_propagates(self):
        """run_scope 接线：pre_think 拦截器返回的新状态被循环采用。"""
        from courtier.agent.core.guardrails import GuardrailSystem

        async def inject(context):
            state = context.state
            marker = Message(role="user", content="【拦截器注入】")
            return state.model_copy(update={"messages": (*state.messages, marker)})

        system = GuardrailSystem()
        system.register_interceptor("pre_think", inject)

        state = AgentState.initial(task="t")
        final = await agent_loop(
            state=state,
            model=MockModelClient(tool_calls=[]),
            guardrail_system=system,
        )

        assert final.status == "completed"
        assert any(m.content == "【拦截器注入】" for m in final.messages)

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
        cm_from_args = call_kwargs.args[1] if len(call_kwargs.args) > 1 else None
        assert cm_from_kwargs is mock_cm or cm_from_args is mock_cm


@pytest.mark.asyncio
async def test_business_artifact_guard_terminates_after_no_progress():
    """After MAX_TURNS_WITHOUT_BUSINESS_ARTIFACTS turns without new business artifacts,
    the loop should terminate."""
    from unittest.mock import AsyncMock, MagicMock

    from courtier.agent.artifacts.models import ArtifactMetadata
    from courtier.agent.artifacts.store import ArtifactStore

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

    store.put(
        Artifact(
            artifact_id="business_1",
            artifact_type="core.plain_text",
            data={"text": "initial"},
            metadata=ArtifactMetadata(created_by="test", debug_only=False),
        )
    )

    tc = ToolCall(id="call_1", name="echo", arguments={})
    # Each turn: model calls echo → produces no new business artifacts
    model = MockModelClient(tool_calls=[tc])

    state = AgentState.initial(task="echo forever", max_steps=20)
    final = await agent_loop(
        state=state,
        model=model,
        tool_registry=registry,
        artifact_store=store,
    )

    # Should terminate — not due to max_steps (20) but due to no business progress
    assert final.status == "completed"
    # The echo tool doesn't create any new artifacts in the store,
    # so after 4 turns without business artifacts the loop should stop
    assert final.current_step < 20


class TestModelErrorEvents:
    @pytest.mark.asyncio
    async def test_model_error_publishes_loop_completed(self):
        """Model failure must still emit loop.completed with error status —
        event consumers (SSE) have no other terminal signal."""
        from courtier.agent.core.event_bus import EventBus

        class _FailingModel:
            model_name = "failing-model"
            temperature = 0.0

            async def generate(self, messages, tools=None, **kwargs):
                raise RuntimeError("Connection refused")

        bus = EventBus()
        sub = bus.subscribe()
        state = AgentState.initial(task="test")
        final = await agent_loop(
            state=state, model=_FailingModel(), tool_registry=None, event_bus=bus
        )

        assert final.status == "error"
        events = []
        while not sub.queue.empty():
            events.append(sub.queue.get_nowait())
        completed = [e for e in events if e.type == "loop.completed"]
        assert len(completed) == 1
        assert completed[0].payload["status"] == "error"
        assert completed[0].payload["termination_reason"]
        assert any(e.type == "state.transition" and e.payload["to"] == "error" for e in events)

    @pytest.mark.asyncio
    async def test_unexpected_phase_error_is_contained(self):
        """An unexpected exception in a phase ends in error state via the
        shared tail (loop.completed published) instead of raising."""
        from unittest.mock import AsyncMock, MagicMock

        from courtier.agent.core.event_bus import EventBus

        mock_cm = MagicMock()
        mock_cm.compact_if_needed = AsyncMock(side_effect=RuntimeError("compact blew up"))

        bus = EventBus()
        sub = bus.subscribe()
        state = AgentState.initial(task="test")
        final = await agent_loop(
            state=state,
            model=MockModelClient(tool_calls=[]),
            tool_registry=None,
            context_manager=mock_cm,
            event_bus=bus,
        )

        assert final.status == "error"
        assert "内部错误" in (final.termination_reason or "")
        events = []
        while not sub.queue.empty():
            events.append(sub.queue.get_nowait())
        assert any(e.type == "loop.completed" for e in events)


class TestToolCallIdEvents:
    """think/tool.start/tool.result events carry tool_call_id so parallel
    same-name calls can be paired to their cards and citations."""

    @pytest.mark.asyncio
    async def test_same_name_parallel_calls_carry_ids(self, registry_with_echo):
        from courtier.agent.core.event_bus import EventBus
        from courtier.agent.core.model import ToolCall

        model = MockModelClient(
            tool_calls=[
                ToolCall(id="call_A", name="echo", arguments={"message": "a"}),
                ToolCall(id="call_B", name="echo", arguments={"message": "b"}),
            ]
        )
        bus = EventBus()
        sub = bus.subscribe()
        state = AgentState.initial(task="test")
        await agent_loop(
            state=state, model=model, tool_registry=registry_with_echo, event_bus=bus
        )
        events = []
        while not sub.queue.empty():
            events.append(sub.queue.get_nowait())

        announced = next(e for e in events if e.type == "think.tool_calls")
        assert announced.payload["names"] == ["echo", "echo"]
        assert announced.payload["ids"] == ["call_A", "call_B"]

        starts = [e for e in events if e.type == "tool.start"]
        assert [e.payload["tool_call_id"] for e in starts] == ["call_A", "call_B"]

        results = [e for e in events if e.type == "tool.result"]
        assert [e.payload["tool_call_id"] for e in results] == ["call_A", "call_B"]

    @pytest.mark.asyncio
    async def test_legacy_callback_without_id_still_works(self, registry_with_echo):
        """A (name)-only legacy on_tool_start callback must not break runs."""
        from courtier.agent.core.model import ToolCall

        seen: list[str] = []

        async def legacy_start(name: str) -> None:
            seen.append(name)

        model = MockModelClient(
            tool_calls=[ToolCall(id="call_X", name="echo", arguments={"message": "x"})]
        )
        state = AgentState.initial(task="test")
        final = await agent_loop(
            state=state,
            model=model,
            tool_registry=registry_with_echo,
            on_tool_start=legacy_start,
        )
        assert final.status == "completed"
        assert seen == ["echo"]


class TestLoopHints:
    def test_append_hint_message_dedupes_identical_hints(self):
        from courtier.agent.core.loop_hints import _append_hint_message

        state = AgentState.initial(task="test")
        state, appended = _append_hint_message(state, "hint-text")
        assert appended is True
        assert state.messages[-1].source == "hint"

        state2, appended2 = _append_hint_message(state, "hint-text")
        assert appended2 is False
        assert state2 is state
        assert sum(1 for m in state.messages if m.content == "hint-text") == 1

    @pytest.mark.asyncio
    async def test_check_and_inject_hints_without_registry_returns_no_reason(self):
        from courtier.agent.core.loop_hints import check_and_inject_hints

        state = AgentState.initial(task="test")
        new_state, reason = await check_and_inject_hints(
            tool_registry=None,
            artifact_store=None,
            consecutive_exploratory=3,
            current_state=state,
        )
        assert reason is None
        assert new_state is state


@pytest.mark.asyncio
class TestForcedFirstToolCall:
    async def _run(self, model, forced, registry):
        from courtier.agent.core.loop import agent_loop
        from courtier.agent.core.state import AgentState

        state = AgentState.initial(task="审核文档", system_prompt="sys")
        return await agent_loop(
            state=state,
            model=model,
            tool_registry=registry,
            forced_first_tool_call=forced,
        )

    @staticmethod
    def _registry(executed: list[str]):
        from courtier.agent.core.execution_result import ExecutionResult
        from courtier.agent.tools.registry import ToolRegistry

        registry = ToolRegistry()

        async def fake_execute(name, **kwargs):
            executed.append(name)
            return ExecutionResult(
                success=True, actor_type="tool", actor_name=name, raw_data={"ok": True}
            )

        registry.execute = fake_execute
        return registry

    async def test_overrides_models_first_calls(self):
        """Model asked for echo first — the forced parse call wins the first turn."""
        from courtier.agent.core.model import MockModelClient, ToolCall

        executed: list[str] = []
        registry = self._registry(executed)
        model = MockModelClient(
            tool_calls=[ToolCall(id="c1", name="echo", arguments={"text": "hi"})]
        )
        forced = ToolCall(
            id="forced-first-parse_layout",
            name="parse_layout",
            arguments={"file_path": "/tmp/x.docx"},
        )

        final = await self._run(model, forced, registry)

        assert executed[0] == "parse_layout"
        tool_msgs = [m for m in final.messages if m.role == "tool"]
        assert tool_msgs[0].tool_call_id == "forced-first-parse_layout"
        # The assistant message carrying the forced call pairs correctly.
        assistant_with_calls = [m for m in final.messages if m.role == "assistant" and m.tool_calls]
        assert assistant_with_calls[0].tool_calls[0].id == "forced-first-parse_layout"

    async def test_no_override_when_model_complies(self):
        """Model already calls parse_layout first — nothing is rewritten."""
        from courtier.agent.core.model import MockModelClient, ToolCall

        executed: list[str] = []
        registry = self._registry(executed)
        model = MockModelClient(
            tool_calls=[
                ToolCall(
                    id="c9",
                    name="parse_layout",
                    arguments={"file_path": "/tmp/x.docx"},
                )
            ]
        )
        forced = ToolCall(
            id="forced-first-parse_layout",
            name="parse_layout",
            arguments={"file_path": "/tmp/x.docx"},
        )

        final = await self._run(model, forced, registry)

        assert executed[0] == "parse_layout"
        tool_msgs = [m for m in final.messages if m.role == "tool"]
        # The model's own call id survived — no synthetic call was injected.
        assert tool_msgs[0].tool_call_id == "c9"

    async def test_injected_when_model_would_answer_directly(self):
        """Without the forced call the loop would end on a text response."""
        from courtier.agent.core.model import MockModelClient, ToolCall

        executed: list[str] = []
        registry = self._registry(executed)
        model = MockModelClient(tool_calls=[])
        forced = ToolCall(
            id="forced-first-parse_layout",
            name="parse_layout",
            arguments={"file_path": "/tmp/x.docx"},
        )

        final = await self._run(model, forced, registry)

        assert executed == ["parse_layout"]
        assert final.status == "completed"

    async def test_override_when_model_calls_same_tool_with_wrong_path(self):
        """Model called parse_layout but for the WRONG file — that is not
        compliance, so the forced call for the session file still wins."""
        from courtier.agent.core.model import MockModelClient, ToolCall

        executed: list[str] = []
        registry = self._registry(executed)
        model = MockModelClient(
            tool_calls=[
                ToolCall(
                    id="c9",
                    name="parse_layout",
                    arguments={"file_path": "/tmp/wrong.docx"},
                )
            ]
        )
        forced = ToolCall(
            id="forced-first-parse_layout-1",
            name="parse_layout",
            arguments={"file_path": "/tmp/x.docx"},
        )

        final = await self._run(model, forced, registry)

        assert executed[0] == "parse_layout"
        tool_msgs = [m for m in final.messages if m.role == "tool"]
        # The model's wrong-path call was replaced by the forced one.
        assert tool_msgs[0].tool_call_id == "forced-first-parse_layout-1"

    async def test_forced_call_survives_failed_first_think(self):
        """A transient model error on the first think must not kill the run
        before the mandatory parse: the forced call still executes and the
        model re-plans with its result on the next turn."""
        from courtier.agent.core.model import MockModelClient, ToolCall

        class _FlakyModel(MockModelClient):
            def __init__(self) -> None:
                super().__init__(tool_calls=[])
                self._failed_once = False

            async def generate(self, messages, tools=None, **kwargs):
                if not self._failed_once:
                    self._failed_once = True
                    raise RuntimeError("transient LLM outage")
                return await super().generate(messages, tools, **kwargs)

        executed: list[str] = []
        registry = self._registry(executed)
        forced = ToolCall(
            id="forced-first-parse_layout-1",
            name="parse_layout",
            arguments={"file_path": "/tmp/x.docx"},
        )

        final = await self._run(_FlakyModel(), forced, registry)

        assert executed == ["parse_layout"]
        assert final.status == "completed"
        assert final.termination_reason != "Model error: transient LLM outage"
        # The injected assistant/tool pair is consistent.
        tool_msgs = [m for m in final.messages if m.role == "tool"]
        assert tool_msgs[0].tool_call_id == "forced-first-parse_layout-1"
        assistant_with_calls = [m for m in final.messages if m.role == "assistant" and m.tool_calls]
        assert assistant_with_calls[0].tool_calls[0].id == "forced-first-parse_layout-1"
