"""Integration tests: EventBus-driven RunRecorder output matches legacy callbacks."""

from __future__ import annotations

import asyncio
import json
import tempfile

import pytest

from courtier.agent.api.services.run_event_log import RunEventLog
from courtier.agent.api.session_store import SessionStore
from courtier.agent.api.sse_adapter import RunRecorder
from courtier.agent.core.event_bus import EventBus
from courtier.agent.core.events import AgentEvent
from courtier.agent.core.execution_result import ExecutionResult


class _LogQueue:
    """Queue-like facade over a RunEventLog for legacy assertion style."""

    def __init__(self, log):
        self.log = log
        self._cursor = -1  # last seq consumed

    def get_nowait(self):
        entries = self.log.replay_after(self._cursor)
        assert entries, "expected another SSE event"
        self._cursor = entries[0].seq
        return ("event", entries[0].line.split("data: ", 1)[1])

    def empty(self):
        return not self.log.replay_after(self._cursor)


def _parse_sse_payload(line: str) -> dict:
    """Strip 'data: ' prefix and parse JSON payload."""
    return json.loads(line.replace("data: ", "").strip())


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as d:
        yield SessionStore(d)


@pytest.mark.asyncio
async def test_event_bus_produces_same_sse_as_legacy_callbacks(store):
    """RunRecorder driven by EventBus emits the same SSE sequence as direct callbacks."""
    session_id = "sess_eb0000000001"
    await store.create(session_id, "task", "file_test1234")

    # Legacy path: call adapter callbacks directly.
    log_legacy_queue = RunEventLog()
    legacy_queue = _LogQueue(log_legacy_queue)
    legacy_adapter = RunRecorder(log_legacy_queue, store, session_id)

    # Event-bus path: publish events to a bus and let the adapter listen.
    bus = EventBus()
    log_eb_queue = RunEventLog()
    eb_queue = _LogQueue(log_eb_queue)
    eb_adapter = RunRecorder(log_eb_queue, store, session_id)
    eb_adapter.start_listening(bus)

    try:
        # Simulate a minimal agent run via events.
        events = [
            AgentEvent(
                type="state.transition",
                session_id=session_id,
                agent_name="agent",
                turn_index=0,
                payload={"to": "thinking", "reason": "text_response"},
            ),
            AgentEvent(
                type="llm.token",
                session_id=session_id,
                agent_name="agent",
                turn_index=0,
                payload={"text": "thinking...", "kind": "reasoning"},
            ),
            AgentEvent(
                type="think.tool_calls",
                session_id=session_id,
                agent_name="agent",
                turn_index=0,
                payload={"names": ["check_format"]},
            ),
            AgentEvent(
                type="tool.start",
                session_id=session_id,
                agent_name="agent",
                turn_index=0,
                payload={"name": "check_format"},
            ),
            AgentEvent(
                type="tool.result",
                session_id=session_id,
                agent_name="agent",
                turn_index=0,
                payload={
                    "name": "check_format",
                    "summary": "found issues",
                    "success": True,
                    "error": None,
                },
            ),
            AgentEvent(
                type="state.transition",
                session_id=session_id,
                agent_name="agent",
                turn_index=0,
                payload={"to": "observing", "reason": "tools_executed"},
            ),
            AgentEvent(
                type="state.transition",
                session_id=session_id,
                agent_name="agent",
                turn_index=1,
                payload={"to": "thinking", "reason": "text_response"},
            ),
            AgentEvent(
                type="llm.content_token",
                session_id=session_id,
                agent_name="agent",
                turn_index=1,
                payload={"text": "Done.", "kind": "content"},
            ),
        ]

        for event in events:
            await bus.publish(event)

        # Drive the legacy adapter with equivalent callbacks.
        await legacy_adapter.on_step("think", "text_response")
        await legacy_adapter.on_token("thinking...")
        await legacy_adapter.on_step("think", "tool_calls: check_format")
        await legacy_adapter.on_tool_start("check_format")
        await legacy_adapter.on_tool_result(
            "check_format",
            ExecutionResult(success=True, actor_type="tool", actor_name="check_format"),
            "found issues",
        )
        await legacy_adapter.on_step("observe", "results_collected")
        await legacy_adapter.on_step("think", "text_response")
        await legacy_adapter.on_content_token("Done.")

        # Give the event-bus listener a moment to process.
        await asyncio.sleep(0.1)

        legacy_payloads = []
        while not legacy_queue.empty():
            tag, line = legacy_queue.get_nowait()
            assert tag == "event"
            legacy_payloads.append(_parse_sse_payload(line))

        eb_payloads = []
        while not eb_queue.empty():
            tag, line = eb_queue.get_nowait()
            assert tag == "event"
            eb_payloads.append(_parse_sse_payload(line))

        assert len(eb_payloads) == len(legacy_payloads)
        for left, right in zip(eb_payloads, legacy_payloads):
            assert left["type"] == right["type"]
    finally:
        eb_adapter.stop_listening()


@pytest.mark.asyncio
async def test_event_bus_tool_error_emits_error_status(store):
    """A tool.error event maps to an SSE tool_result with status=error."""
    session_id = "sess_eb0000000002"
    await store.create(session_id, "task", "file_test1234")

    bus = EventBus()
    log_queue = RunEventLog()
    queue = _LogQueue(log_queue)
    adapter = RunRecorder(log_queue, store, session_id)
    adapter.start_listening(bus)

    try:
        await bus.publish(
            AgentEvent(
                type="state.transition",
                session_id=session_id,
                agent_name="agent",
                turn_index=0,
                payload={"to": "waiting_for_tool", "reason": "tool_calls: fail_tool"},
            )
        )
        await bus.publish(
            AgentEvent(
                type="tool.error",
                session_id=session_id,
                agent_name="agent",
                turn_index=0,
                payload={
                    "name": "fail_tool",
                    "summary": "failed",
                    "success": False,
                    "error": "boom",
                },
            )
        )
        await asyncio.sleep(0.05)

        payloads = []
        while not queue.empty():
            tag, line = queue.get_nowait()
            payloads.append(_parse_sse_payload(line))

        tool_result = [p for p in payloads if p.get("type") == "tool_result"]
        assert len(tool_result) == 1
        assert tool_result[0]["status"] == "error"
    finally:
        adapter.stop_listening()


@pytest.mark.asyncio
async def test_event_bus_usage_event(store):
    """An llm.usage event maps to an SSE usage event."""
    session_id = "sess_eb0000000003"
    await store.create(session_id, "task", "file_test1234")

    bus = EventBus()
    log_queue = RunEventLog()
    queue = _LogQueue(log_queue)
    adapter = RunRecorder(log_queue, store, session_id)
    adapter.start_listening(bus)

    try:
        await bus.publish(
            AgentEvent(
                type="llm.usage",
                session_id=session_id,
                agent_name="agent",
                turn_index=0,
                payload={"prompt_tokens": 100, "completion_tokens": 50},
            )
        )
        await asyncio.sleep(0.05)

        payloads = []
        while not queue.empty():
            tag, line = queue.get_nowait()
            payloads.append(_parse_sse_payload(line))

        usage = [p for p in payloads if p.get("type") == "usage"]
        assert len(usage) == 1
        assert usage[0]["tokensIn"] == 100
        assert usage[0]["tokensOut"] == 50

        session = await store.get(session_id)
        assert session is not None
        assert session.tokens_in == 100
        assert session.tokens_out == 50
    finally:
        adapter.stop_listening()


@pytest.mark.asyncio
async def test_agent_loop_emits_structured_think_tool_calls(store):
    """End-to-end regression: a tool-calling turn must emit think.tool_calls
    so the SSE think event carries the tool names.

    Previously the adapter only saw the bare "tool_calls" state-transition
    reason (no names) and mis-mapped the turn to a text_response step, so
    parent-level tool cards were silently dropped by the frontend.
    """
    from courtier.agent.core.loop import agent_loop
    from courtier.agent.core.model import ToolCall
    from courtier.agent.core.state import AgentState
    from courtier.agent.testing import MockModelClient
    from courtier.agent.tools.registry import ToolRegistry

    session_id = "sess_eb0000000004"
    await store.create(session_id, "task", None)

    bus = EventBus()
    log_queue = RunEventLog()
    queue = _LogQueue(log_queue)
    adapter = RunRecorder(log_queue, store, session_id)
    adapter.start_listening(bus)

    class _EchoTool:
        name = "echo"
        description = "echo"
        parameters = {"type": "object", "properties": {}}

        async def execute(self, **kwargs):
            return ExecutionResult(
                success=True, actor_type="tool", actor_name="echo", raw_data="hi"
            )

    registry = ToolRegistry()
    registry.register(_EchoTool())

    try:
        model = MockModelClient(tool_calls=[ToolCall(id="c1", name="echo", arguments={})])
        final = await agent_loop(
            state=AgentState.initial(task="trace"),
            model=model,
            tool_registry=registry,
            event_bus=bus,
            session_id=session_id,
        )
        assert final.status == "completed"
        await asyncio.sleep(0.1)

        payloads = []
        while not queue.empty():
            tag, line = queue.get_nowait()
            assert tag == "event"
            payloads.append(_parse_sse_payload(line))

        think_events = [p for p in payloads if p.get("type") == "think"]
        # The first think event is the pre-stream placeholder so reasoning
        # tokens have a step from the very first token.
        assert think_events[0].get("textResponse")
        # The tool-calling turn announces the tool by name.
        assert any(p.get("toolCalls") == ["echo"] for p in think_events)
        # text_response placeholders: one per turn (pre-stream), never
        # replacing the tool-call announcement (the original bug).
        text_resp = [p for p in think_events if p.get("textResponse")]
        assert len(text_resp) <= 2

        # The persisted steps must carry the tool (the placeholder step from
        # the pre-stream announcement may precede it).
        session = await store.get(session_id)
        assert session is not None
        steps = session.steps or []
        assert steps, "expected at least one stored step"
        assert any(s.label == "echo" for s in steps)
        tool_results = [p for p in payloads if p.get("type") == "tool_result"]
        assert len(tool_results) == 1
        assert tool_results[0]["name"] == "echo"
    finally:
        adapter.stop_listening()
