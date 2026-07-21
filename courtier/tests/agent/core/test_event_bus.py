"""Tests for the agent event bus and agent_loop event publishing."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import pytest

from courtier.agent.core.event_bus import EventBus
from courtier.agent.core.events import AgentEvent
from courtier.agent.core.loop import agent_loop
from courtier.agent.core.model import MockModelClient, ToolCall
from courtier.agent.core.state import AgentState
from courtier.agent.tools.builtin.echo import EchoTool
from courtier.agent.tools.registry import ToolRegistry


class _UsageMockModelClient(MockModelClient):
    """Mock model that reports token usage in every response."""

    async def generate(self, messages, tools=None, **kwargs):  # type: ignore[no-untyped-def]
        response = await super().generate(messages, tools=tools, **kwargs)
        return replace(
            response,
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        )


class TestEventBus:
    @pytest.mark.asyncio
    async def test_publish_and_subscribe(self):
        bus = EventBus()
        sub = bus.subscribe()

        event = AgentEvent(
            type="llm.token",
            session_id="sess_1",
            agent_name="agent",
            turn_index=0,
            payload={"text": "hello"},
        )
        await bus.publish(event)

        received = await asyncio.wait_for(sub.queue.get(), timeout=1.0)
        assert received.event_id == event.event_id
        assert received.type == "llm.token"

    @pytest.mark.asyncio
    async def test_filter_by_event_type(self):
        bus = EventBus()
        sub = bus.subscribe(event_types={"llm.token", "tool.result"})

        await bus.publish(
            AgentEvent(
                type="llm.token",
                session_id="sess_1",
                agent_name="agent",
                turn_index=0,
                payload={"text": "a"},
            )
        )
        await bus.publish(
            AgentEvent(
                type="state.transition",
                session_id="sess_1",
                agent_name="agent",
                turn_index=0,
                payload={"to": "thinking"},
            )
        )

        received = await asyncio.wait_for(sub.queue.get(), timeout=1.0)
        assert received.type == "llm.token"
        assert sub.queue.empty()

    @pytest.mark.asyncio
    async def test_filter_by_session_id(self):
        bus = EventBus()
        sub = bus.subscribe(session_id="sess_a")

        await bus.publish(
            AgentEvent(
                type="llm.token",
                session_id="sess_a",
                agent_name="agent",
                turn_index=0,
                payload={},
            )
        )
        await bus.publish(
            AgentEvent(
                type="llm.token",
                session_id="sess_b",
                agent_name="agent",
                turn_index=0,
                payload={},
            )
        )

        received = await asyncio.wait_for(sub.queue.get(), timeout=1.0)
        assert received.session_id == "sess_a"
        assert sub.queue.empty()

    @pytest.mark.asyncio
    async def test_multiple_subscribers_receive_event(self):
        bus = EventBus()
        sub1 = bus.subscribe()
        sub2 = bus.subscribe()

        event = AgentEvent(
            type="loop.completed",
            session_id="sess_1",
            agent_name="agent",
            turn_index=0,
            payload={"status": "completed"},
        )
        await bus.publish(event)

        r1 = await asyncio.wait_for(sub1.queue.get(), timeout=1.0)
        r2 = await asyncio.wait_for(sub2.queue.get(), timeout=1.0)
        assert r1.event_id == event.event_id
        assert r2.event_id == event.event_id

    @pytest.mark.asyncio
    async def test_drop_oldest_backpressure(self):
        bus = EventBus(default_maxsize=2, backpressure="drop_oldest")
        sub = bus.subscribe()

        for i in range(3):
            await bus.publish(
                AgentEvent(
                    type="llm.token",
                    session_id="sess_1",
                    agent_name="agent",
                    turn_index=0,
                    payload={"text": str(i)},
                )
            )

        items = []
        while not sub.queue.empty():
            items.append(await asyncio.wait_for(sub.queue.get(), timeout=1.0))

        assert len(items) == 2
        assert items[0].payload["text"] == "1"
        assert items[1].payload["text"] == "2"

    @pytest.mark.asyncio
    async def test_drop_newest_backpressure(self):
        bus = EventBus(default_maxsize=2, backpressure="drop_newest")
        sub = bus.subscribe()

        for i in range(3):
            await bus.publish(
                AgentEvent(
                    type="llm.token",
                    session_id="sess_1",
                    agent_name="agent",
                    turn_index=0,
                    payload={"text": str(i)},
                )
            )

        items = []
        while not sub.queue.empty():
            items.append(await asyncio.wait_for(sub.queue.get(), timeout=1.0))

        assert len(items) == 2
        assert items[0].payload["text"] == "0"
        assert items[1].payload["text"] == "1"

    @pytest.mark.asyncio
    async def test_unsubscribe_removes_listener(self):
        bus = EventBus()
        sub = bus.subscribe()
        bus.unsubscribe(sub)

        await bus.publish(
            AgentEvent(
                type="llm.token",
                session_id="sess_1",
                agent_name="agent",
                turn_index=0,
                payload={},
            )
        )
        assert sub.queue.empty()


class TestAgentLoopEvents:
    @pytest.mark.asyncio
    async def test_agent_loop_publishes_events(self):
        bus = EventBus()
        sub = bus.subscribe(session_id="sess_1")

        state = AgentState.initial(task="say hello", system_prompt="You are a helper.")
        model = MockModelClient(tool_calls=[])

        final_state = await agent_loop(
            state=state,
            model=model,
            session_id="sess_1",
            agent_name="test_agent",
            event_bus=bus,
        )

        assert final_state.status == "completed"

        # Drain events
        events: list[AgentEvent] = []
        await asyncio.sleep(0.05)
        while not sub.queue.empty():
            events.append(sub.queue.get_nowait())

        event_types = [e.type for e in events]
        assert "state.transition" in event_types
        assert "llm.request" in event_types
        assert "llm.response" in event_types
        assert "loop.completed" in event_types

    @pytest.mark.asyncio
    async def test_agent_loop_publishes_structured_usage_event(self):
        """llm.usage is published directly with a structured payload.

        The legacy on_step("usage", "prompt,completion") string protocol was
        removed; the loop now emits the event from the think-phase metrics
        block instead.
        """
        bus = EventBus()
        sub = bus.subscribe(session_id="sess_usage")

        legacy_steps: list[tuple[str, str]] = []

        async def on_step(event: str, detail: str) -> None:
            legacy_steps.append((event, detail))

        state = AgentState.initial(task="say hello", system_prompt="You are a helper.")
        model = _UsageMockModelClient(tool_calls=[])

        await agent_loop(
            state=state,
            model=model,
            session_id="sess_usage",
            agent_name="test_agent",
            event_bus=bus,
            on_step=on_step,
        )

        await asyncio.sleep(0.05)
        usage_events = [e for e in sub.queue._queue if e.type == "llm.usage"]
        assert len(usage_events) == 1
        assert usage_events[0].payload == {
            "prompt_tokens": 10,
            "completion_tokens": 5,
        }
        # The legacy "usage" step event is gone — on_step never sees it.
        assert not any(event == "usage" for event, _ in legacy_steps)

    @pytest.mark.asyncio
    async def test_agent_loop_event_bus_and_legacy_callbacks(self):
        bus = EventBus()
        sub = bus.subscribe(session_id="sess_2")

        callback_calls: list[tuple[str, Any]] = []

        async def on_token(token: str) -> None:
            callback_calls.append(("token", token))

        state = AgentState.initial(task="say hello", system_prompt="You are a helper.")
        model = MockModelClient(tool_calls=[])

        await agent_loop(
            state=state,
            model=model,
            session_id="sess_2",
            agent_name="test_agent",
            event_bus=bus,
            on_token=on_token,
        )

        # Legacy callback should still be invoked alongside event publishing.
        assert any(t == "token" for t, _ in callback_calls)

        # Event bus should also contain the token events (mock streaming emits
        # one character at a time).
        await asyncio.sleep(0.05)
        token_events = [e for e in sub.queue._queue if e.type == "llm.token"]
        assert len(token_events) == 5
        assert "".join(e.payload["text"] for e in token_events) == "Done."

    @pytest.mark.asyncio
    async def test_agent_loop_without_event_bus_uses_legacy_callbacks(self):
        callback_calls: list[tuple[str, Any]] = []

        async def on_step(event: str, detail: str) -> None:
            callback_calls.append(("step", event, detail))

        async def on_token(token: str) -> None:
            callback_calls.append(("token", token))

        state = AgentState.initial(task="say hello", system_prompt="You are a helper.")
        model = MockModelClient(tool_calls=[])

        await agent_loop(
            state=state,
            model=model,
            session_id="sess_3",
            agent_name="test_agent",
            on_step=on_step,
            on_token=on_token,
        )

        assert any(c[0] == "token" for c in callback_calls)
        assert any(c[0] == "step" for c in callback_calls)

    @pytest.mark.asyncio
    async def test_agent_loop_tool_events(self):
        bus = EventBus()
        sub = bus.subscribe(session_id="sess_4")

        state = AgentState.initial(task="call echo", system_prompt="You are a helper.")
        model = MockModelClient(
            tool_calls=[ToolCall(id="call_1", name="echo", arguments={"text": "hi"})]
        )

        registry = ToolRegistry()
        registry.register(EchoTool())

        final_state = await agent_loop(
            state=state,
            model=model,
            tool_registry=registry,
            session_id="sess_4",
            agent_name="test_agent",
            event_bus=bus,
        )

        assert final_state.status == "completed"

        await asyncio.sleep(0.05)
        events = list(sub.queue._queue)
        event_types = [e.type for e in events]
        assert "tool.start" in event_types
        assert "tool.result" in event_types
