"""Integration test: verify tool_result SSE events complete a tool call."""

import asyncio
import json
import tempfile

import pytest

from courtier.agent.agents.base import Agent
from courtier.agent.api.session_store import SessionStore
from courtier.agent.api.sse_adapter import SSEAdapter
from courtier.agent.core.model import MockModelClient, ToolCall
from courtier.agent.tools.builtin.echo import EchoTool
from courtier.agent.tools.registry import ToolRegistry


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as d:
        yield SessionStore(d)


@pytest.mark.asyncio
async def test_echo_tool_result_event_sequence(store):
    """A single echo tool call should produce think, act, tool_start,
    tool_result, observe and complete events.
    """
    q: asyncio.Queue = asyncio.Queue()
    session_id = "sess_7e57e57e57e5"
    await store.create(session_id, "echo hello", "")

    registry = ToolRegistry()
    registry.register(EchoTool())

    adapter = SSEAdapter(q, store, session_id, tool_registry=registry)

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
