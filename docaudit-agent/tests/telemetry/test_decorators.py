# tests/telemetry/test_decorators.py
"""Tests for tracing decorators."""

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from courtier.agent.telemetry.decorators import traced_agent, traced_llm, traced_tool


@pytest.fixture(autouse=True)
def setup_telemetry():
    provider = TracerProvider()
    trace.set_tracer_provider(provider)


class TestTracedAgent:
    async def test_decorated_method_returns_result(self):
        @traced_agent("test-agent")
        async def fake_run(self, task: str):
            return {"result": task}

        class FakeAgent:
            pass

        agent = FakeAgent()
        result = await fake_run(agent, task="hello")
        assert result == {"result": "hello"}

    async def test_decorated_method_passes_kwargs(self):
        @traced_agent("test-agent")
        async def fake_run(self, task: str, extra: str = ""):
            return task + extra

        class FakeAgent:
            pass

        result = await fake_run(FakeAgent(), task="a", extra="b")
        assert result == "ab"


class TestTracedLLM:
    async def test_decorated_llm_call_returns_result(self):
        class FakeUsage:
            prompt_tokens = 10
            completion_tokens = 5

        class FakeResponse:
            usage = FakeUsage()

        @traced_llm(model="test-model")
        async def fake_llm(prompt: str):
            return FakeResponse()

        result = await fake_llm(prompt="test")
        assert result.usage.prompt_tokens == 10

    async def test_decorated_llm_without_usage(self):
        @traced_llm(model="test-model")
        async def fake_llm(prompt: str):
            return "plain string"

        result = await fake_llm(prompt="test")
        assert result == "plain string"


class TestTracedTool:
    async def test_decorated_tool_returns_result(self):
        @traced_tool("test-tool")
        async def fake_tool(query: str):
            return f"result: {query}"

        result = await fake_tool(query="search")
        assert result == "result: search"

    async def test_decorated_tool_exception_raises(self):
        @traced_tool("test-tool")
        async def fake_tool(query: str):
            raise ValueError("bad input")

        with pytest.raises(ValueError, match="bad input"):
            await fake_tool(query="bad")
