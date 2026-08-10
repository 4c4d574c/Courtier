"""Test: think_phase emits a compact event when full compaction shrinks history."""

from __future__ import annotations

import pytest

from courtier.agent.core.loop_phases import think_phase
from courtier.agent.core.model import MockModelClient
from courtier.agent.core.state import AgentState


class _CompactingContextManager:
    """Stand-in that always shrinks the message list (real compaction)."""

    async def compact_if_needed(self, messages, *, on_compact_start=None):
        if on_compact_start is not None:
            await on_compact_start()
        return messages[:1]


class _NoopContextManager:
    """Stand-in that leaves messages untouched (no compaction)."""

    async def compact_if_needed(self, messages, *, on_compact_start=None):
        return messages


@pytest.mark.asyncio
async def test_compact_event_emitted_when_history_shrinks():
    events: list[tuple[str, str]] = []

    async def on_step(event: str, detail: str) -> None:
        events.append((event, detail))

    state = AgentState.initial(task="审核文档", system_prompt="sys")
    assert len(state.messages) == 2

    result = await think_phase(
        state=state,
        model=MockModelClient(tool_calls=[]),
        tool_registry=None,
        context_manager=_CompactingContextManager(),
        recent_reasoning=[],
        on_step=on_step,
        on_token=None,
        on_content_token=None,
    )

    compact_events = [d for e, d in events if e == "compact"]
    assert compact_events == ["2 条消息 → 1 条"]
    # 压缩开始通知必须先于完成通知到达
    event_names = [e for e, _ in events]
    assert event_names.index("compacting") < event_names.index("compact")
    # The compacted history went into the model call; the model's reply is
    # appended afterwards.
    assert result.state.messages[0].content == "sys"
    assert result.state.messages[-1].role == "assistant"


@pytest.mark.asyncio
async def test_compact_event_published_on_event_bus():
    """生产路径（事件总线驱动）也收到压缩通知——此前 compact 只走 legacy
    on_step 回调，而 stream_service 纯总线驱动，前端永远收不到。"""
    from courtier.agent.core.event_bus import EventBus
    from courtier.agent.core.loop import agent_loop

    bus = EventBus()
    sub = bus.subscribe()
    state = AgentState.initial(task="审核文档", system_prompt="sys")
    await agent_loop(
        state=state,
        model=MockModelClient(tool_calls=[]),
        tool_registry=None,
        context_manager=_CompactingContextManager(),
        event_bus=bus,
    )

    events = []
    while not sub.queue.empty():
        events.append(sub.queue.get_nowait())
    compacted = [e for e in events if e.type == "context.compacted"]
    assert len(compacted) == 1
    # agent_loop 在首个 think 前会注入额外上下文消息，条数不锁定具体值。
    assert compacted[0].payload["detail"].endswith("→ 1 条")
    # 开始压缩的事件也走总线（前端据此显示"正在压缩"指示）
    compacting = [e for e in events if e.type == "context.compacting"]
    assert len(compacting) == 1
    assert events.index(compacting[0]) < events.index(compacted[0])


@pytest.mark.asyncio
async def test_no_compact_event_when_unchanged():
    events: list[tuple[str, str]] = []

    async def on_step(event: str, detail: str) -> None:
        events.append((event, detail))

    state = AgentState.initial(task="审核文档", system_prompt="sys")
    await think_phase(
        state=state,
        model=MockModelClient(tool_calls=[]),
        tool_registry=None,
        context_manager=_NoopContextManager(),
        recent_reasoning=[],
        on_step=on_step,
        on_token=None,
        on_content_token=None,
    )

    assert [d for e, d in events if e == "compact"] == []
    # 未发生压缩时同样不得发出"正在压缩"通知
    assert [d for e, d in events if e == "compacting"] == []
