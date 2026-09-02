"""Refusal retry policy in the think phase (refusal plan T2)."""

from __future__ import annotations

import pytest

from courtier.agent.core.loop_phases import think_phase
from courtier.agent.core.model import ModelResponse
from courtier.agent.core.state import AgentState, ToolCall
from courtier.config import get_settings


class _SequentialModel:
    model_name = "stub"
    temperature = 0.0

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    async def generate(self, messages, tools=None, **kwargs):
        self.calls += 1
        return self.responses[min(self.calls - 1, len(self.responses) - 1)]

    async def generate_stream_full(self, messages, tools=None, **kwargs):
        return await self.generate(messages, tools=tools, **kwargs)


def _refusal() -> ModelResponse:
    return ModelResponse(content="很抱歉，我无法协助完成该操作。", tool_calls=[])


def _ok(text: str = "任务已完成。") -> ModelResponse:
    return ModelResponse(content=text, tool_calls=[])


@pytest.fixture
def refusal_settings(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "refusal_detection_enabled", True)
    monkeypatch.setattr(settings, "refusal_retry_max", 1)
    monkeypatch.setattr(settings, "refusal_patterns", ["我无法"])
    return settings


@pytest.mark.asyncio
async def test_retry_recovers_on_second_attempt(refusal_settings):
    model = _SequentialModel([_refusal(), _ok("已完成。")])
    state = AgentState.initial("task")

    async def on_step(event, detail):
        pass

    result = await think_phase(
        state=state,
        model=model,
        tool_registry=None,
        context_manager=None,
        recent_reasoning=[],
        on_step=on_step,
        on_token=None,
        on_content_token=None,
        publish=None,
    )
    assert model.calls == 2
    assert result.llm_response.content == "已完成。"
    assert result.refusal_attempts == 1
    assert result.refusal_exhausted is None
    # The refused response never enters the conversation history.
    assistant_contents = [
        m.content for m in result.state.messages if m.role == "assistant"
    ]
    assert assistant_contents == ["已完成。"]


@pytest.mark.asyncio
async def test_exhausted_after_budget_spent(refusal_settings):
    refusal_settings.refusal_retry_max = 1
    model = _SequentialModel([_refusal(), _refusal()])

    async def on_step(event, detail):
        pass

    result = await think_phase(
        state=AgentState.initial("task"),
        model=model,
        tool_registry=None,
        context_manager=None,
        recent_reasoning=[],
        on_step=on_step,
        on_token=None,
        on_content_token=None,
        publish=None,
    )
    assert model.calls == 2
    assert result.refusal_attempts == 1
    assert result.refusal_exhausted == "我无法"


@pytest.mark.asyncio
async def test_zero_retries_only_detects(refusal_settings):
    refusal_settings.refusal_retry_max = 0
    model = _SequentialModel([_refusal()])

    async def on_step(event, detail):
        pass

    result = await think_phase(
        state=AgentState.initial("task"),
        model=model,
        tool_registry=None,
        context_manager=None,
        recent_reasoning=[],
        on_step=on_step,
        on_token=None,
        on_content_token=None,
        publish=None,
    )
    assert model.calls == 1
    assert result.refusal_attempts == 0
    assert result.refusal_exhausted is None
    assert result.llm_response.content is not None


@pytest.mark.asyncio
async def test_usage_accumulates_across_attempts(refusal_settings):
    refusal_settings.refusal_retry_max = 1
    refused = ModelResponse(
        content="我无法完成。",
        tool_calls=[],
        usage={"prompt_tokens": 100, "completion_tokens": 10},
    )
    ok = ModelResponse(
        content="好的。",
        tool_calls=[],
        usage={"prompt_tokens": 120, "completion_tokens": 5},
    )
    model = _SequentialModel([refused, ok])

    async def on_step(event, detail):
        pass

    result = await think_phase(
        state=AgentState.initial("task"),
        model=model,
        tool_registry=None,
        context_manager=None,
        recent_reasoning=[],
        on_step=on_step,
        on_token=None,
        on_content_token=None,
        publish=None,
    )
    assert result.llm_response.usage["prompt_tokens"] == 220
    assert result.llm_response.usage["completion_tokens"] == 15


@pytest.mark.asyncio
async def test_tool_call_responses_never_detected(refusal_settings):
    model = _SequentialModel(
        [ModelResponse(content="我无法完成。", tool_calls=[ToolCall(id="1", name="echo", arguments={})])]
    )

    async def on_step(event, detail):
        pass

    result = await think_phase(
        state=AgentState.initial("task"),
        model=model,
        tool_registry=None,
        context_manager=None,
        recent_reasoning=[],
        on_step=on_step,
        on_token=None,
        on_content_token=None,
        publish=None,
    )
    assert model.calls == 1
    assert result.refusal_attempts == 0
    assert result.refusal_exhausted is None


@pytest.mark.asyncio
async def test_publish_events_order(refusal_settings):
    events = []

    async def publish(event_type, payload):
        events.append((event_type, payload))

    model = _SequentialModel([_refusal(), _refusal()])

    async def on_step(event, detail):
        pass

    await think_phase(
        state=AgentState.initial("task"),
        model=model,
        tool_registry=None,
        context_manager=None,
        recent_reasoning=[],
        on_step=on_step,
        on_token=None,
        on_content_token=None,
        publish=publish,
    )
    types = [t for t, _ in events]
    assert types == [
        "refusal.detected",
        "think.retry",
        "refusal.detected",
        "refusal.exhausted",
    ]
    exhausted = dict(events[-1][1])
    assert "多次拒绝" in exhausted["text"] or "unavailable" in exhausted["text"]
    assert exhausted["matched"] == "我无法"


@pytest.mark.asyncio
async def test_zero_retries_never_publishes_exhausted(refusal_settings):
    refusal_settings.refusal_retry_max = 0
    events = []

    async def publish(event_type, payload):
        events.append(event_type)

    model = _SequentialModel([_refusal()])

    async def on_step(event, detail):
        pass

    await think_phase(
        state=AgentState.initial("task"),
        model=model,
        tool_registry=None,
        context_manager=None,
        recent_reasoning=[],
        on_step=on_step,
        on_token=None,
        on_content_token=None,
        publish=publish,
    )
    assert events == ["refusal.detected"]
