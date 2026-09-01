"""Tests for AgentRuntime."""

import asyncio

import pytest

from courtier.agent.core.model import ModelResponse
from courtier.agent.runtime import AgentRuntime, AgentRuntimeBudget
from courtier.agent.runtime.handle import AgentHandle
from courtier.agent.runtime.result import ExecutionResult
from courtier.agent.skills import SkillRegistry
from courtier.agent.testing import MockModelClient
from courtier.agent.tools.builtin.echo import EchoTool
from courtier.agent.tools.registry import ToolRegistry


def _make_runtime(tmp_path, skill_names, model=None, default_budget=None):
    """Create an AgentRuntime whose agents are registered from skill files."""
    for name in skill_names:
        (tmp_path / f"{name}.md").write_text(
            f"---\nname: {name}\nversion: '1.0'\ntools: [echo]\n---\nYou are {name}.\n",
            encoding="utf-8",
        )
    skill_registry = SkillRegistry(tmp_path)
    skill_registry.scan()

    tool_registry = ToolRegistry()
    tool_registry.register(EchoTool())

    kwargs = {}
    if default_budget is not None:
        kwargs["default_budget"] = default_budget
    return AgentRuntime(
        tool_registry=tool_registry,
        model=model if model is not None else MockModelClient(tool_calls=[]),
        skill_registry=skill_registry,
        **kwargs,
    )


@pytest.fixture
def runtime_with_echo():
    reg = ToolRegistry()
    reg.register(EchoTool())
    model = MockModelClient(tool_calls=[])
    return AgentRuntime(tool_registry=reg, model=model)


@pytest.fixture
def runtime_with_skill_agent(tmp_path):
    return _make_runtime(tmp_path, ["echo_agent"])


@pytest.mark.asyncio
async def test_spawn_unknown_agent_raises(runtime_with_echo):
    with pytest.raises(ValueError, match="未知的子代理或技能"):
        runtime_with_echo.spawn(name="missing", task="test")


@pytest.mark.asyncio
async def test_spawn_returns_handle(runtime_with_skill_agent):
    handle = runtime_with_skill_agent.spawn(name="echo_agent", task="say hi")
    assert isinstance(handle, AgentHandle)
    assert handle.agent_name == "echo_agent"
    assert handle.task == "say hi"


@pytest.mark.asyncio
async def test_spawn_preserves_context(runtime_with_skill_agent):
    handle = runtime_with_skill_agent.spawn(
        name="echo_agent",
        task="say hi",
        context={"file_path": "/tmp/doc.pdf"},
    )
    assert handle.context == {"file_path": "/tmp/doc.pdf"}


@pytest.mark.asyncio
async def test_delegate_propagates_context_to_system_prompt(runtime_with_skill_agent):
    from unittest.mock import patch

    with patch("courtier.agent.agents.base.Agent.build_system_prompt") as mock_build:
        mock_build.return_value = ""
        handle = runtime_with_skill_agent.spawn(
            name="echo_agent",
            task="say hi",
            context={"file_path": "/tmp/doc.pdf"},
        )
        await runtime_with_skill_agent.delegate(handle)

    assert mock_build.called
    call_args = mock_build.call_args
    call_context = call_args.args[0] if call_args.args else call_args.kwargs.get("context")
    assert call_context == {"file_path": "/tmp/doc.pdf"}


@pytest.mark.asyncio
async def test_spawn_respects_budget_depth(tmp_path):
    runtime = _make_runtime(
        tmp_path,
        ["loop_agent"],
        default_budget=AgentRuntimeBudget(max_depth=1),
    )

    parent = runtime.spawn(name="loop_agent", task="outer")
    with pytest.raises(RuntimeError, match="嵌套深度"):
        runtime.spawn(name="loop_agent", task="inner", parent_handle=parent)


@pytest.mark.asyncio
async def test_spawn_detects_cycle(tmp_path):
    runtime = _make_runtime(tmp_path, ["loop_agent"])

    parent = runtime.spawn(name="loop_agent", task="outer")
    with pytest.raises(RuntimeError, match="启动环"):
        runtime.spawn(name="loop_agent", task="inner", parent_handle=parent)


@pytest.mark.asyncio
async def test_delegate_returns_execution_result(runtime_with_skill_agent):
    handle = runtime_with_skill_agent.spawn(name="echo_agent", task="say hi")
    result = await runtime_with_skill_agent.delegate(handle)
    assert isinstance(result, ExecutionResult)
    assert result.actor_name == "echo_agent"


@pytest.mark.asyncio
async def test_delegate_emits_events(runtime_with_skill_agent):
    events = []

    async def on_event(event):
        events.append(event.kind)

    handle = runtime_with_skill_agent.spawn(name="echo_agent", task="say hi")
    await runtime_with_skill_agent.delegate(handle, on_subagent_event=on_event)

    assert "start" in events
    assert "end" in events


@pytest.mark.asyncio
async def test_terminate_cancels_running_task(tmp_path):
    runtime = _make_runtime(tmp_path, ["slow_agent"])

    handle = runtime.spawn(name="slow_agent", task="wait")
    # terminate is a no-op if delegate has not started the task
    await runtime.terminate(handle)


class _HangingModelClient(MockModelClient):
    """Mock client that never returns until cancelled."""

    def __init__(self) -> None:
        super().__init__(tool_calls=[])

    async def generate(self, messages, tools=None, **kwargs):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise
        return ModelResponse(content="Done.", tool_calls=[])

    async def generate_stream_full(self, messages, tools=None, **kwargs):
        return await self.generate(messages, tools=tools, **kwargs)


@pytest.mark.asyncio
async def test_terminate_cancels_running_delegate(tmp_path):
    runtime = _make_runtime(tmp_path, ["slow_agent"], model=_HangingModelClient())

    handle = runtime.spawn(name="slow_agent", task="wait")
    delegate_task = asyncio.create_task(runtime.delegate(handle))
    await asyncio.sleep(0.05)
    await runtime.terminate(handle)
    result = await delegate_task

    assert isinstance(result, ExecutionResult)
    assert not result.success
    assert "子代理已被终止" in result.error


@pytest.mark.asyncio
async def test_spawn_parent_budget_overrides_explicit_budget(tmp_path):
    runtime = _make_runtime(
        tmp_path,
        ["parent_agent", "child_agent"],
        default_budget=AgentRuntimeBudget(max_depth=1),
    )

    parent = runtime.spawn(name="parent_agent", task="outer")
    # An explicit permissive budget should be ignored when a parent is supplied.
    explicit_budget = AgentRuntimeBudget(max_depth=5)
    with pytest.raises(RuntimeError, match="嵌套深度"):
        runtime.spawn(
            name="child_agent",
            task="inner",
            parent_handle=parent,
            budget=explicit_budget,
        )


@pytest.mark.asyncio
async def test_delegate_enforces_cumulative_runtime_budget(tmp_path):
    runtime = _make_runtime(
        tmp_path,
        ["agent"],
        default_budget=AgentRuntimeBudget(max_cumulative_runtime_seconds=0.0),
    )

    handle = runtime.spawn(name="agent", task="work")
    result = await runtime.delegate(handle)

    assert isinstance(result, ExecutionResult)
    assert not result.success
    assert "累计运行时间预算已耗尽" in result.error


@pytest.mark.asyncio
async def test_delegate_chains_callbacks(runtime_with_skill_agent):
    subagent_events = []
    original_tokens = []

    async def on_subagent_event(event):
        subagent_events.append(event.kind)

    async def on_token(token):
        original_tokens.append(token)

    handle = runtime_with_skill_agent.spawn(name="echo_agent", task="say hi")
    result = await runtime_with_skill_agent.delegate(
        handle,
        on_subagent_event=on_subagent_event,
        callbacks={"on_token": on_token},
    )

    assert isinstance(result, ExecutionResult)
    assert "token" in subagent_events
    assert len(original_tokens) > 0


@pytest.mark.asyncio
async def test_top_level_spawns_use_distinct_cumulative_runtime_keys(tmp_path):
    runtime = _make_runtime(
        tmp_path,
        ["agent"],
        default_budget=AgentRuntimeBudget(max_cumulative_runtime_seconds=10.0),
    )

    first = runtime.spawn(name="agent", task="first")
    second = runtime.spawn(name="agent", task="second")

    await runtime.delegate(first)
    await runtime.delegate(second)

    # Two distinct root keys should exist in cumulative runtime tracking.
    assert len(runtime._cumulative_runtime) == 2


@pytest.mark.asyncio
async def test_cumulative_runtime_recorded_on_termination(tmp_path):
    runtime = _make_runtime(
        tmp_path,
        ["slow_agent"],
        model=_HangingModelClient(),
        default_budget=AgentRuntimeBudget(max_cumulative_runtime_seconds=10.0),
    )

    handle = runtime.spawn(name="slow_agent", task="wait")
    delegate_task = asyncio.create_task(runtime.delegate(handle))
    await asyncio.sleep(0.05)
    await runtime.terminate(handle)
    await delegate_task

    assert runtime._cumulative_runtime[handle.handle_id] > 0


def test_build_agent_scopes_tools_for_skill(tmp_path):
    runtime = _make_runtime(tmp_path, ["scoped_agent"])

    built = runtime._build_agent(runtime._configs["scoped_agent"])
    tool_names = {t.name for t in built.tool_registry.list_tools()}
    assert "echo" in tool_names


@pytest.mark.asyncio
async def test_delegate_blackbox_suppresses_intermediate_events(runtime_with_skill_agent):
    events = []

    async def on_event(event):
        events.append(event.kind)

    handle = runtime_with_skill_agent.spawn(
        name="echo_agent", task="say hi", context_mode="blackbox"
    )
    await runtime_with_skill_agent.delegate(handle, on_subagent_event=on_event)

    assert "start" in events
    assert "end" in events
    assert "token" not in events
    assert "think" not in events
    assert "tool_result" not in events


@pytest.mark.asyncio
async def test_delegate_transparent_forwards_intermediate_events(runtime_with_skill_agent):
    events = []

    async def on_event(event):
        events.append(event.kind)

    handle = runtime_with_skill_agent.spawn(
        name="echo_agent", task="say hi", context_mode="transparent"
    )
    await runtime_with_skill_agent.delegate(handle, on_subagent_event=on_event)

    assert "start" in events
    assert "end" in events
    assert "token" in events


@pytest.mark.asyncio
async def test_events_carry_scope_id(runtime_with_skill_agent):
    events = []

    async def on_event(event):
        events.append(event)

    handle = runtime_with_skill_agent.spawn(
        name="echo_agent", task="say hi", context_mode="transparent"
    )
    await runtime_with_skill_agent.delegate(handle, on_subagent_event=on_event)

    assert all(e.scope_id == handle.scope_id for e in events if e.scope_id is not None)


@pytest.mark.asyncio
async def test_event_bus_receives_subagent_events(runtime_with_skill_agent):
    from courtier.agent.core.events import AgentEvent

    runtime = runtime_with_skill_agent
    sub = runtime.event_bus.subscribe(event_types={"subagent.event"})

    handle = runtime.spawn(name="echo_agent", task="say hi")
    await runtime.delegate(handle)

    received = []
    while not sub.queue.empty():
        received.append(sub.queue.get_nowait())

    assert any(isinstance(e, AgentEvent) and e.type == "subagent.event" for e in received)


def test_spawn_assigns_distinct_scope_ids(runtime_with_skill_agent):
    h1 = runtime_with_skill_agent.spawn(name="echo_agent", task="a")
    h2 = runtime_with_skill_agent.spawn(name="echo_agent", task="b")
    assert h1.scope_id is not None
    assert h2.scope_id is not None
    assert h1.scope_id != h2.scope_id
