"""Integration test: verify tool_result SSE events complete a tool call."""

import asyncio
import json
import tempfile

import pytest

from courtier.agent.agents.base import Agent
from courtier.agent.api.services.run_event_log import RunEventLog
from courtier.agent.api.session_store import SessionStore
from courtier.agent.api.sse_adapter import RunRecorder
from courtier.agent.core.model import MockModelClient, ToolCall
from courtier.agent.tools.builtin.echo import EchoTool
from courtier.agent.tools.registry import ToolRegistry


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


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as d:
        yield SessionStore(d)


@pytest.mark.asyncio
async def test_echo_tool_result_event_sequence(store):
    """A single echo tool call should produce think, act, tool_start,
    tool_result, observe and complete events.
    """
    log_q = RunEventLog()
    q = _LogQueue(log_q)
    session_id = "sess_7e57e57e57e5"
    await store.create(session_id, "echo hello", "")

    registry = ToolRegistry()
    registry.register(EchoTool())

    adapter = RunRecorder(log_q, store, session_id, tool_registry=registry)

    tc = ToolCall(id="1", name="echo", arguments={"text": "hello"})
    model = MockModelClient(tool_calls=[tc])

    agent = Agent(
        name="EchoAgent",
        role="Echo back text using the echo tool.",
        tools=[EchoTool()],
        model=model,
    )

    await agent.run(
        "echo hello",
        on_step=adapter.on_step,
        on_token=adapter.on_token,
        on_content_token=adapter.on_content_token,
        on_tool_start=adapter.on_tool_start,
        on_tool_progress=lambda name, p: asyncio.create_task(adapter.on_tool_progress(name, p)),
        on_tool_result=adapter.on_tool_result,
    )

    # Drain the queue
    events = []
    while not q.empty():
        item = q.get_nowait()
        if item[0] == "event":
            data_line = item[1].replace("data: ", "").strip()
            events.append(json.loads(data_line))

    types = [e["type"] for e in events]
    print("Event sequence:", types)

    # Assertions
    assert "think" in types
    assert "act" in types
    assert "tool_start" in types
    assert "tool_result" in types
    assert "observe" in types

    tool_result = next(e for e in events if e["type"] == "tool_result")
    assert tool_result["name"] == "echo"
    assert tool_result["status"] == "ok"

    # Verify SSE events are ordered correctly.
    assert types.index("tool_result") < types.index("observe")

    # Verify session store reflects completed tool (stored as "ok" internally).
    session = await store.get(session_id)
    assert session is not None
    tool_records = [t for s in session.steps for t in s.tools]
    assert any(t.name == "echo" and t.status == "ok" for t in tool_records)


@pytest.mark.asyncio
async def test_same_name_parallel_calls_pair_by_tool_call_id(store):
    """Two same-name tool calls in one turn: every SSE event and stored
    ToolInfo carries the tool_call_id so the frontend can pair results to
    their cards (name-based matching reverses the pairing)."""
    log_q = RunEventLog()
    q = _LogQueue(log_q)
    session_id = "sess_5a6e7a8e9a0e"
    await store.create(session_id, "echo twice", "")

    registry = ToolRegistry()
    registry.register(EchoTool())

    adapter = RunRecorder(log_q, store, session_id, tool_registry=registry)

    model = MockModelClient(
        tool_calls=[
            ToolCall(id="call_a", name="echo", arguments={"text": "first"}),
            ToolCall(id="call_b", name="echo", arguments={"text": "second"}),
        ]
    )
    agent = Agent(
        name="EchoAgent",
        role="Echo back text using the echo tool.",
        tools=[EchoTool()],
        model=model,
    )
    await agent.run(
        "echo twice",
        on_step=adapter.on_step,
        on_token=adapter.on_token,
        on_content_token=adapter.on_content_token,
        on_tool_start=adapter.on_tool_start,
        on_tool_progress=lambda name, p: asyncio.create_task(adapter.on_tool_progress(name, p)),
        on_tool_result=adapter.on_tool_result,
    )

    events = []
    while not q.empty():
        item = q.get_nowait()
        if item[0] == "event":
            data_line = item[1].replace("data: ", "").strip()
            events.append(json.loads(data_line))

    think = next(e for e in events if e["type"] == "think" and e.get("toolCalls"))
    assert think["toolCalls"] == ["echo", "echo"]
    # The legacy callback path announces names only; toolCallIds ride the
    # think.tool_calls bus event (asserted separately below).

    starts = [e for e in events if e["type"] == "tool_start"]
    assert [e["toolCallId"] for e in starts] == ["call_a", "call_b"]

    results = [e for e in events if e["type"] == "tool_result"]
    assert [e["toolCallId"] for e in results] == ["call_a", "call_b"]

    # Parallel same-name durations are keyed per call, not per name.
    assert len({e["duration"] for e in results}) >= 1  # both defined

    session = await store.get(session_id)
    stored = [t for s in session.steps for t in s.tools]
    assert sorted(t.tool_call_id for t in stored) == ["call_a", "call_b"]


@pytest.mark.asyncio
async def test_bus_think_tool_calls_announces_ids(store):
    """The bus path (production wiring via start_listening) announces
    toolCallIds alongside toolCalls on the think event."""
    log_q = RunEventLog()
    q = _LogQueue(log_q)
    session_id = "sess_b051d5"
    await store.create(session_id, "bus ids", "")
    adapter = RunRecorder(log_q, store, session_id)

    from courtier.agent.core.event_bus import EventBus

    bus = EventBus()
    adapter.start_listening(bus)
    from courtier.agent.core.events import AgentEvent

    await bus.publish(
        AgentEvent(
            type="think.tool_calls",
            session_id=session_id,
            agent_name="Courtier",
            turn_index=0,
            payload={
                "names": ["search_documents", "search_documents"],
                "ids": ["c1", "c2"],
            },
        )
    )
    await asyncio.sleep(0.05)

    events = []
    while not q.empty():
        item = q.get_nowait()
        events.append(json.loads(item[1].replace("data: ", "").strip()))
    think = next(e for e in events if e["type"] == "think" and e.get("toolCalls"))
    assert think["toolCallIds"] == ["c1", "c2"]


def _make_bus_agent(session_id: str):
    from courtier.agent.core.event_bus import EventBus
    from courtier.agent.core.events import AgentEvent

    bus = EventBus()

    async def publish(payload: dict, event_type: str = "tool.result") -> None:
        await bus.publish(
            AgentEvent(
                type=event_type,
                session_id=session_id,
                agent_name="Courtier",
                turn_index=0,
                payload=payload,
            )
        )

    return bus, publish


def _drain(log_q: RunEventLog, cursor: int = -1) -> list[dict]:
    import json as _json

    events = []
    for entry in log_q.replay_after(cursor):
        events.append(_json.loads(entry.line.split("data: ", 1)[1]))
    return events


@pytest.mark.asyncio
async def test_bus_tool_result_builds_detail_from_real_result(store):
    """The loop payload carries the real ExecutionResult; the recorder builds
    the expanded-card detail from it instead of the empty-shell fallback."""
    from courtier.agent.core.execution_result import ExecutionResult

    log_q = RunEventLog()
    session_id = "sess_b051d5aa0001"
    await store.create(session_id, "bus detail", "")
    adapter = RunRecorder(log_q, store, session_id)
    bus, publish = _make_bus_agent(session_id)
    adapter.start_listening(bus)

    await publish({"names": ["activate_domain"], "ids": ["c1"]}, "think.tool_calls")
    await publish(
        {
            "name": "activate_domain",
            "summary": "完成 (2 个字段)",
            "success": True,
            "result": ExecutionResult(
                success=True,
                actor_type="tool",
                actor_name="activate_domain",
                raw_data={"skills": "格式审核", "rules": "GB/T 9704"},
            ),
        }
    )
    await asyncio.sleep(0.05)

    events = _drain(log_q)
    tool_result = next(e for e in events if e["type"] == "tool_result")
    assert tool_result["detail_data"]["type"] == "structured"
    assert tool_result["detail_data"]["data"]["skills"] == "格式审核"

    session = await store.get(session_id)
    stored = [t for s in session.steps for t in s.tools]
    record = next(t for t in stored if t.name == "activate_domain")
    assert record.detail == {
        "type": "structured",
        "data": {"skills": "格式审核", "rules": "GB/T 9704"},
    }


@pytest.mark.asyncio
async def test_bus_tool_result_persisted_result_detail(store):
    """Persisted results (raw_data dropped for a $ref) surface their key
    excerpts: text payloads as markdown, JSON payloads as structured rows."""
    from courtier.agent.core.execution_result import ExecutionResult

    log_q = RunEventLog()
    session_id = "sess_b051d5bb0002"
    await store.create(session_id, "bus persisted detail", "")
    adapter = RunRecorder(log_q, store, session_id)
    bus, publish = _make_bus_agent(session_id)
    adapter.start_listening(bus)

    await publish({"names": ["convert_document"], "ids": ["c1"]}, "think.tool_calls")
    await publish(
        {
            "name": "convert_document",
            "summary": "完成 (已持久化: $ref:convert_document:1)",
            "success": True,
            "result": ExecutionResult(
                success=True,
                actor_type="tool",
                actor_name="convert_document",
                result_id="convert_document:1",
                content_type="text/plain",
                key_excerpts=("# 财政部文件", "第二条 …"),
            ),
        }
    )
    await publish(
        {
            "name": "content_audit",
            "summary": "完成 (已持久化: $ref:content_audit:1)",
            "success": True,
            "result": ExecutionResult(
                success=True,
                actor_type="tool",
                actor_name="content_audit",
                result_id="content_audit:1",
                content_type="application/json",
                key_excerpts=("问题位置: 第2段",),
            ),
        }
    )
    await asyncio.sleep(0.05)

    events = _drain(log_q)
    by_name = {e["name"]: e for e in events if e["type"] == "tool_result"}
    assert by_name["convert_document"]["detail_data"] == {
        "type": "markdown",
        "content": "# 财政部文件\n\n第二条 …",
    }
    assert by_name["content_audit"]["detail_data"]["type"] == "structured"
    assert by_name["content_audit"]["detail_data"]["data"]["$ref"] == "content_audit:1"
    assert by_name["content_audit"]["detail_data"]["data"]["excerpt_1"] == "问题位置: 第2段"

    session = await store.get(session_id)
    stored = {t.name: t for s in session.steps for t in s.tools}
    assert stored["convert_document"].detail["type"] == "markdown"
    assert stored["content_audit"].detail["data"]["$ref"] == "content_audit:1"


def test_build_detail_data_persisted_without_content_returns_none():
    """A persisted result with neither excerpts nor stored preview has no
    detail to show — None keeps the frontend summary-only fallback."""
    from courtier.agent.api.sse_adapter import RunRecorder
    from courtier.agent.core.execution_result import ExecutionResult

    result = ExecutionResult(
        success=True,
        actor_type="tool",
        actor_name="x",
        result_id="ref:1",
    )
    assert RunRecorder._build_detail_data(result) is None
