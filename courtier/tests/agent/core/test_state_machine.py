"""Tests for AgentStateMachine."""

from __future__ import annotations

import pytest

from courtier.agent.core.event_bus import EventBus
from courtier.agent.core.state import AgentState
from courtier.agent.core.state_machine import AgentStateMachine, InvalidStateTransition


class TestAgentStateMachine:
    def test_valid_transition_idle_to_thinking(self):
        sm = AgentStateMachine()
        state = AgentState.initial(task="test")
        new_state = sm.transition(state, "thinking", "turn_start")
        assert new_state.status == "thinking"

    def test_valid_transition_thinking_to_waiting_for_tool(self):
        sm = AgentStateMachine()
        state = AgentState.initial(task="test").model_copy(update={"status": "thinking"})
        new_state = sm.transition(state, "waiting_for_tool", "tool_calls")
        assert new_state.status == "waiting_for_tool"

    def test_valid_transition_waiting_for_tool_to_observing(self):
        sm = AgentStateMachine()
        state = AgentState.initial(task="test").model_copy(
            update={"status": "waiting_for_tool"}
        )
        new_state = sm.transition(state, "observing", "tools_executed")
        assert new_state.status == "observing"

    def test_invalid_transition_raises_in_strict_mode(self):
        sm = AgentStateMachine(strict=True)
        state = AgentState.initial(task="test")
        with pytest.raises(InvalidStateTransition):
            sm.transition(state, "completed", "bad")

    def test_invalid_transition_is_allowed_in_non_strict_mode(self):
        sm = AgentStateMachine(strict=False)
        state = AgentState.initial(task="test")
        new_state = sm.transition(state, "completed", "bad")
        assert new_state.status == "completed"

    def test_no_op_transition_preserves_state(self):
        sm = AgentStateMachine()
        state = AgentState.initial(task="test").model_copy(update={"status": "thinking"})
        new_state = sm.transition(state, "thinking", "noop")
        assert new_state is state

    def test_terminal_reason_set_for_completed(self):
        sm = AgentStateMachine()
        state = AgentState.initial(task="test").model_copy(update={"status": "thinking"})
        new_state = sm.transition(state, "completed", "max_steps")
        assert new_state.termination_reason == "max_steps"

    def test_terminal_reason_set_for_error(self):
        sm = AgentStateMachine()
        state = AgentState.initial(task="test").model_copy(update={"status": "thinking"})
        new_state = sm.transition(state, "error", "model_error")
        assert new_state.termination_reason == "model_error"

    def test_can_transition_helper(self):
        sm = AgentStateMachine()
        assert sm.can_transition("idle", "thinking")
        assert sm.can_transition("thinking", "waiting_for_tool")
        assert not sm.can_transition("completed", "thinking")

    def test_is_terminal(self):
        sm = AgentStateMachine()
        assert sm.is_terminal("completed")
        assert sm.is_terminal("blocked")
        assert sm.is_terminal("error")
        assert not sm.is_terminal("thinking")

    @pytest.mark.asyncio
    async def test_transition_async_emits_event_and_sets_transition_id(self):
        bus = EventBus()
        sub = bus.subscribe(event_types={"state.transition"})
        sm = AgentStateMachine(
            strict=False,
            event_bus=bus,
            session_id="s1",
            agent_name="a1",
        )
        state = AgentState.initial(task="test").model_copy(update={"status": "thinking"})
        new_state = await sm.transition_async(state, "completed", "max_steps")
        assert new_state.transition_id is not None
        event = await sub.queue.get()
        assert event.type == "state.transition"
        assert event.payload["transition_id"] == new_state.transition_id
        assert event.payload["from"] == "thinking"
        assert event.payload["to"] == "completed"
        assert event.session_id == "s1"
        assert event.agent_name == "a1"

    @pytest.mark.asyncio
    async def test_transition_async_no_event_for_no_op(self):
        bus = EventBus()
        sub = bus.subscribe(event_types={"state.transition"})
        sm = AgentStateMachine(event_bus=bus)
        state = AgentState.initial(task="test").model_copy(update={"status": "thinking"})
        new_state = await sm.transition_async(state, "thinking", "noop")
        assert new_state is state
        assert sub.queue.empty()
