"""Tests for AgentState."""

import json

import pytest

from courtier.agent.core.execution_result import ExecutionResult
from courtier.agent.core.model import ModelResponse, ToolCall
from courtier.agent.core.state import AgentState, Message


class TestMessage:
    def test_to_openai_dict_user_message(self):
        msg = Message(role="user", content="hello")
        d = msg.to_openai_dict()
        assert d == {"role": "user", "content": "hello"}

    def test_to_openai_dict_assistant_with_tool_calls(self):
        tc = ToolCall(id="1", name="echo", arguments={"text": "hello"})
        msg = Message(role="assistant", content=None, tool_calls=[tc])
        d = msg.to_openai_dict()
        assert d["role"] == "assistant"
        assert d["tool_calls"][0]["id"] == "1"
        assert d["tool_calls"][0]["function"]["name"] == "echo"
        # arguments must be valid JSON string
        parsed = json.loads(d["tool_calls"][0]["function"]["arguments"])
        assert parsed == {"text": "hello"}

    def test_to_openai_dict_tool_result_message(self):
        msg = Message(
            role="tool", content='{"ok": true}', tool_call_id="call_1", name="echo"
        )
        d = msg.to_openai_dict()
        assert d["role"] == "tool"
        assert d["tool_call_id"] == "call_1"
        assert d["name"] == "echo"

    def test_to_openai_dict_content_none_omitted(self):
        msg = Message(role="assistant")
        d = msg.to_openai_dict()
        assert "content" not in d


class TestAgentState:
    def test_initial_state(self):
        state = AgentState.initial(task="test task", system_prompt="You are helpful.")
        assert state.status == "idle"
        assert len(state.messages) == 2
        assert state.messages[0].role == "system"
        assert state.messages[1].role == "user"
        assert state.messages[1].content == "test task"

    def test_initial_state_no_system_prompt(self):
        state = AgentState.initial(task="test")
        assert len(state.messages) == 1
        assert state.messages[0].role == "user"

    def test_is_terminal(self):
        idle = AgentState.initial(task="test")
        assert not idle.is_terminal()
        completed = idle.model_copy(update={"status": "completed"})
        assert completed.is_terminal()
        blocked = idle.model_copy(update={"status": "blocked"})
        assert blocked.is_terminal()
        error = idle.model_copy(update={"status": "error"})
        assert error.is_terminal()

    def test_add_thought_with_tool_calls(self):
        state = AgentState.initial(task="test")
        tc = ToolCall(id="1", name="echo", arguments={"text": "hello"})
        response = ModelResponse(content=None, tool_calls=[tc])

        new_state = state.add_thought(response)
        assert new_state.status == "waiting_for_tool"
        assert len(new_state.tool_calls) == 1
        assert new_state.tool_calls[0].name == "echo"
        assert new_state.current_step == 1

        # last message should be assistant with tool_calls
        assert new_state.messages[-1].role == "assistant"
        assert new_state.messages[-1].tool_calls == (tc,)

    def test_add_thought_without_tool_calls(self):
        state = AgentState.initial(task="test")
        response = ModelResponse(content="Hello!", tool_calls=[])

        new_state = state.add_thought(response)
        assert new_state.status == "completed"
        assert new_state.termination_reason == "completed"
        assert new_state.messages[-1].content == "Hello!"

    def test_add_thought_on_terminal_state_returns_self(self):
        state = AgentState.initial(task="test")
        terminal = state.model_copy(
            update={"status": "completed", "termination_reason": "done"}
        )
        response = ModelResponse(content="ignored")
        result = terminal.add_thought(response)
        assert result is terminal  # identity preserved

    def test_add_observation(self):
        state = AgentState.initial(task="test")
        tc = ToolCall(id="1", name="echo", arguments={"text": "hello"})
        state = state.add_thought(ModelResponse(content=None, tool_calls=[tc]))

        result = ExecutionResult(
            success=True, actor_type="tool", actor_name="echo", raw_data="hello"
        )
        new_state = state.add_observation((result,))
        assert new_state.status == "observing"
        assert new_state.tool_results[0].raw_data == "hello"
        assert len(new_state.tool_calls) == 0
        # last message should be tool result
        assert new_state.messages[-1].role == "tool"

    def test_add_observation_multiple_tools(self):
        state = AgentState.initial(task="test")
        tc1 = ToolCall(id="1", name="echo", arguments={"text": "a"})
        tc2 = ToolCall(id="2", name="echo", arguments={"text": "b"})
        state = state.add_thought(ModelResponse(content=None, tool_calls=[tc1, tc2]))

        r1 = ExecutionResult(success=True, actor_type="tool", actor_name="echo", raw_data="a")
        r2 = ExecutionResult(success=True, actor_type="tool", actor_name="echo", raw_data="b")
        new_state = state.add_observation((r1, r2))
        assert new_state.status == "observing"
        assert len(new_state.tool_results) == 2
        assert new_state.messages[-2].name == "echo"
        assert new_state.messages[-1].name == "echo"

    def test_add_observation_result_count_mismatch_returns_error(self):
        state = AgentState.initial(task="test")
        tc = ToolCall(id="1", name="echo", arguments={"text": "hi"})
        state = state.add_thought(ModelResponse(content=None, tool_calls=[tc]))

        with pytest.raises(ValueError, match="Result count mismatch"):
            state.add_observation(
                (
                    ExecutionResult(success=True, actor_type="tool", actor_name="echo"),
                    ExecutionResult(success=True, actor_type="tool", actor_name="echo"),
                )
            )

    def test_max_steps_guard(self):
        state = AgentState.initial(task="test", max_steps=2)
        # Step 1: tool call
        tc = ToolCall(id="1", name="echo", arguments={"text": "hi"})
        state = state.add_thought(ModelResponse(content=None, tool_calls=[tc]))

        # Step 2: final response — hits max_steps
        state = state.add_thought(ModelResponse(content="Done", tool_calls=[]))
        assert state.status == "completed"
        assert state.termination_reason == "max_steps"

    def test_max_steps_strips_tool_calls_from_assistant_message(self):
        """Pending calls dropped by max_steps must not linger in the stored
        history — a dangling assistant(tool_calls) message breaks the OpenAI
        message-sequence contract when the session is resumed."""
        state = AgentState.initial(task="test", max_steps=1)
        tc = ToolCall(id="1", name="echo", arguments={"text": "hi"})
        new_state = state.add_thought(ModelResponse(content=None, tool_calls=[tc]))

        assert new_state.status == "completed"
        assert new_state.termination_reason == "max_steps"
        assert new_state.tool_calls == ()
        assistant = new_state.messages[-1]
        assert assistant.role == "assistant"
        assert assistant.tool_calls is None

    def test_to_openai_messages_drops_unanswered_tool_calls(self):
        """Assistant tool_calls without tool results are sanitized for the API."""
        tc = ToolCall(id="1", name="echo", arguments={"text": "hi"})
        state = AgentState.initial(task="test")
        dangling = Message(role="assistant", content=None, tool_calls=(tc,))
        state = state.model_copy(update={"messages": state.messages + (dangling,)})

        msgs = state.to_openai_messages()
        assert "tool_calls" not in msgs[-1]

    def test_to_openai_messages_keeps_answered_tool_calls(self):
        """A normal think/act turn keeps its tool_calls in the API payload."""
        tc = ToolCall(id="1", name="echo", arguments={"text": "hi"})
        state = AgentState.initial(task="test")
        state = state.add_thought(ModelResponse(content=None, tool_calls=[tc]))
        result = ExecutionResult(success=True, actor_type="tool", actor_name="echo")
        state = state.add_observation((result,))

        msgs = state.to_openai_messages()
        assistant = msgs[-2]
        assert assistant["role"] == "assistant"
        assert assistant["tool_calls"][0]["id"] == "1"
        assert msgs[-1]["role"] == "tool"

    def test_blocked(self):
        state = AgentState.initial(task="test")
        new_state = state.blocked("Permission denied: delete_rule")
        assert new_state.status == "blocked"
        assert "Permission denied" in (new_state.termination_reason or "")

    def test_errored(self):
        state = AgentState.initial(task="test")
        new_state = state.errored("Network timeout")
        assert new_state.status == "error"
        assert new_state.termination_reason == "Network timeout"

    def test_to_openai_messages(self):
        state = AgentState.initial(task="hello", system_prompt="Be helpful.")
        msgs = state.to_openai_messages()
        assert len(msgs) == 2
        assert msgs[0]["role"] == "system"
        assert msgs[1]["role"] == "user"
        assert msgs[1]["content"] == "hello"
