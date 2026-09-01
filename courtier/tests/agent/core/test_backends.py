"""Tests for ModelBackend, ModelRouter, and BackendModelClient."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from courtier.agent.core.backends.openai_backend import OpenAIModelBackend
from courtier.agent.core.backends.router import ModelRouter, ModelUnavailable, RoutingStrategy
from courtier.agent.core.loop import agent_loop
from courtier.agent.core.model import BackendModelClient, ModelResponse
from courtier.agent.core.protocol import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    TokenChunk,
    TokenUsage,
    ToolCall,
)
from courtier.agent.core.state import AgentState
from courtier.agent.telemetry.metrics import MODEL_STREAM_TOOL_CALLS_LOST_TOTAL


class FixedModelBackend:
    """Test backend that returns a predetermined response."""

    name = "fixed"
    supports_tool_calls = True
    supports_streaming = True

    def __init__(
        self,
        *,
        content: str | None = None,
        tool_calls: list[ToolCall] | None = None,
        reasoning: str | None = None,
        fail: bool = False,
        latency_ms: float = 1.0,
    ) -> None:
        self._content = content
        self._tool_calls = tool_calls or []
        self._reasoning = reasoning
        self._fail = fail
        self._latency_ms = latency_ms
        self._model = "fixed-model"
        self._temperature = 0.5

    async def chat(self, request: ChatRequest) -> ChatResponse:
        if self._fail:
            raise ModelUnavailable(self.name, "simulated failure")
        return ChatResponse(
            backend=self.name,
            model=self._model,
            message=ChatMessage(
                role="assistant",
                content=self._content,
                tool_calls=self._tool_calls or None,
                reasoning_content=self._reasoning,
            ),
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5),
            latency_ms=self._latency_ms,
        )

    async def stream(
        self, request: ChatRequest
    ) -> AsyncIterator[TokenChunk | ChatResponse]:
        if self._fail:
            raise ModelUnavailable(self.name, "simulated failure")
        if self._content:
            yield TokenChunk(text=self._content, kind="content")
        yield ChatResponse(
            backend=self.name,
            model=self._model,
            message=ChatMessage(
                role="assistant",
                content=self._content,
                tool_calls=self._tool_calls or None,
                reasoning_content=self._reasoning,
            ),
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5),
            latency_ms=self._latency_ms,
        )


class ToolCallDroppingStreamBackend:
    """Stream reports finish_reason="tool_calls" but yields no tool call data."""

    name = "dropping"
    supports_tool_calls = True
    supports_streaming = True

    def __init__(self) -> None:
        self.chat_calls = 0

    async def chat(self, request: ChatRequest) -> ChatResponse:
        self.chat_calls += 1
        return ChatResponse(
            backend=self.name,
            model="dropping-model",
            message=ChatMessage(
                role="assistant",
                tool_calls=[ToolCall(id="c1", name="echo", arguments={})],
            ),
            finish_reason="tool_calls",
        )

    async def stream(
        self, request: ChatRequest
    ) -> AsyncIterator[TokenChunk | ChatResponse]:
        yield ChatResponse(
            backend=self.name,
            model="dropping-model",
            message=ChatMessage(role="assistant"),
            finish_reason="tool_calls",
        )


class TestBackendModelClient:
    @pytest.mark.asyncio
    async def test_generate_delegates_to_backend(self):
        backend = FixedModelBackend(content="Hello from backend")
        client = BackendModelClient(backend=backend, model="fixed-model", temperature=0.5)

        response = await client.generate(messages=[{"role": "user", "content": "hi"}])

        assert isinstance(response, ModelResponse)
        assert response.content == "Hello from backend"

    @pytest.mark.asyncio
    async def test_generate_with_tool_calls(self):
        backend = FixedModelBackend(
            tool_calls=[ToolCall(id="c1", name="echo", arguments={"text": "hi"})]
        )
        client = BackendModelClient(backend=backend, model="fixed-model")

        response = await client.generate(messages=[{"role": "user", "content": "call echo"}])

        assert len(response.tool_calls) == 1
        assert response.tool_calls[0].name == "echo"

    @pytest.mark.asyncio
    async def test_generate_propagates_reasoning_content(self):
        backend = FixedModelBackend(content="hi", reasoning="thinking out loud")
        client = BackendModelClient(backend=backend, model="fixed-model")

        response = await client.generate(messages=[{"role": "user", "content": "hi"}])

        assert response.reasoning_content == "thinking out loud"

    @pytest.mark.asyncio
    async def test_stream_full_delegates_to_backend(self):
        backend = FixedModelBackend(content="Hi")
        client = BackendModelClient(backend=backend, model="fixed-model")

        tokens: list[str] = []

        async def on_content_token(t: str) -> None:
            tokens.append(t)

        response = await client.generate_stream_full(
            messages=[{"role": "user", "content": "hi"}],
            on_content_token=on_content_token,
        )

        assert response.content == "Hi"
        assert tokens == ["Hi"]

    @pytest.mark.asyncio
    async def test_stream_full_falls_back_when_tool_calls_dropped(self):
        """vLLM 流式返回 finish_reason="tool_calls" 但无数据时回落非流式。"""
        backend = ToolCallDroppingStreamBackend()
        client = BackendModelClient(backend=backend, model="dropping-model")
        tools = [{"type": "function", "function": {"name": "echo"}}]

        response = await client.generate_stream_full(
            messages=[{"role": "user", "content": "call echo"}], tools=tools
        )

        assert backend.chat_calls == 1
        assert len(response.tool_calls) == 1
        assert response.tool_calls[0].name == "echo"

    @pytest.mark.asyncio
    async def test_stream_full_fallback_records_metric(self):
        """流式 tool_calls 丢失回落时递增 model_stream_tool_calls_lost_total。"""
        backend = ToolCallDroppingStreamBackend()
        client = BackendModelClient(backend=backend, model="dropping-model")
        tools = [{"type": "function", "function": {"name": "echo"}}]

        counter = MODEL_STREAM_TOOL_CALLS_LOST_TOTAL.labels(model="dropping-model")
        before = counter._value.get()
        await client.generate_stream_full(
            messages=[{"role": "user", "content": "call echo"}], tools=tools
        )
        assert counter._value.get() == before + 1


class TestModelRouter:
    @pytest.mark.asyncio
    async def test_close_closes_all_backends(self):
        class ClosableBackend(FixedModelBackend):
            def __init__(self) -> None:
                super().__init__()
                self.closed = False

            async def close(self) -> None:
                self.closed = True

        first, second = ClosableBackend(), ClosableBackend()
        router = ModelRouter([first, second])

        await router.close()

        assert first.closed and second.closed

    @pytest.mark.asyncio
    async def test_primary_strategy_uses_first_backend(self):
        primary = FixedModelBackend(content="primary")
        fallback = FixedModelBackend(content="fallback")
        router = ModelRouter([primary, fallback])

        request = ChatRequest(model="m", messages=())
        response = await router.chat(request)

        assert response.message.content == "primary"

    @pytest.mark.asyncio
    async def test_primary_strategy_falls_back(self):
        primary = FixedModelBackend(content="primary", fail=True)
        fallback = FixedModelBackend(content="fallback")
        router = ModelRouter([primary, fallback])

        request = ChatRequest(model="m", messages=())
        response = await router.chat(request)

        assert response.message.content == "fallback"

    @pytest.mark.asyncio
    async def test_all_backends_fail_raises(self):
        router = ModelRouter([FixedModelBackend(fail=True)])
        with pytest.raises(Exception):
            await router.chat(ChatRequest(model="m", messages=()))

    @pytest.mark.asyncio
    async def test_cost_strategy_routes_long_input_to_second_backend(self):
        cheap = FixedModelBackend(content="cheap")
        expensive = FixedModelBackend(content="expensive")
        router = ModelRouter(
            [cheap, expensive],
            RoutingStrategy(name="cost", cost_threshold_chars=10),
        )

        short = ChatRequest(model="m", messages=(ChatMessage(role="user", content="hi"),))
        long = ChatRequest(
            model="m",
            messages=(ChatMessage(role="user", content="this is a long message"),),
        )

        assert (await router.chat(short)).message.content == "cheap"
        assert (await router.chat(long)).message.content == "expensive"

    @pytest.mark.asyncio
    async def test_quality_strategy_routes_tools_to_second_backend(self):
        simple = FixedModelBackend(content="simple")
        capable = FixedModelBackend(content="capable")
        router = ModelRouter(
            [simple, capable],
            RoutingStrategy(name="quality"),
        )

        no_tools = ChatRequest(model="m", messages=())
        with_tools = ChatRequest(
            model="m",
            messages=(),
            tools=[{"type": "function", "function": {}}],
        )

        assert (await router.chat(no_tools)).message.content == "simple"
        assert (await router.chat(with_tools)).message.content == "capable"

    @pytest.mark.asyncio
    async def test_stream_uses_first_available_backend(self):
        primary = FixedModelBackend(content="primary")
        fallback = FixedModelBackend(content="fallback")
        router = ModelRouter([primary, fallback])

        request = ChatRequest(model="m", messages=())
        chunks = [chunk async for chunk in router.stream(request)]

        assert len(chunks) == 2  # TokenChunk + ChatResponse
        assert chunks[0].text == "primary"
        # FixedModelBackend reports its own name as the backend in ChatResponse.
        assert chunks[1].backend == "fixed"

    @pytest.mark.asyncio
    async def test_stream_falls_back_when_primary_fails(self):
        primary = FixedModelBackend(content="primary", fail=True)
        fallback = FixedModelBackend(content="fallback")
        router = ModelRouter([primary, fallback])

        request = ChatRequest(model="m", messages=())
        chunks = [chunk async for chunk in router.stream(request)]

        assert len(chunks) == 2
        assert chunks[0].text == "fallback"
        assert chunks[1].backend == "fixed"

    @pytest.mark.asyncio
    async def test_stream_skips_non_streaming_backend(self):
        class NonStreamingBackend:
            name = "non_streaming"
            supports_tool_calls = True
            supports_streaming = False

            async def chat(self, request: ChatRequest) -> ChatResponse:
                return ChatResponse(
                    backend=self.name,
                    model="m",
                    message=ChatMessage(role="assistant", content="sync"),
                    usage=TokenUsage(prompt_tokens=1, completion_tokens=1),
                )

            async def stream(self, request: ChatRequest):
                raise RuntimeError("should not be called")

        streaming = FixedModelBackend(content="streaming")
        router = ModelRouter([NonStreamingBackend(), streaming])

        request = ChatRequest(model="m", messages=())
        chunks = [chunk async for chunk in router.stream(request)]

        assert len(chunks) == 2
        assert chunks[0].text == "streaming"


class TestAgentLoopWithBackend:
    @pytest.mark.asyncio
    async def test_agent_loop_with_backend_model_client(self):
        backend = FixedModelBackend(content="Done.")
        client = BackendModelClient(backend=backend, model="fixed-model")

        state = AgentState.initial(task="say hi")
        final = await agent_loop(state=state, model=client)

        assert final.status == "completed"
        assert final.messages[-1].content == "Done."

    @pytest.mark.asyncio
    async def test_agent_loop_with_router_backend(self):
        primary = FixedModelBackend(content="Done.", fail=True)
        fallback = FixedModelBackend(content="Fallback done.")
        router = ModelRouter([primary, fallback])
        client = BackendModelClient(backend=router, model="routed")

        state = AgentState.initial(task="say hi")
        final = await agent_loop(state=state, model=client)

        assert final.status == "completed"
        assert final.messages[-1].content == "Fallback done."


class TestOpenAIModelBackendParams:
    """参数组装：max_tokens / extra_body 在 ChatRequest 上的映射优先级。"""

    def _backend(self, **kwargs: Any) -> OpenAIModelBackend:
        return OpenAIModelBackend(
            base_url="http://localhost:1", api_key="test-key", **kwargs
        )

    def test_request_max_tokens_takes_precedence(self):
        backend = self._backend(max_tokens=100)
        request = ChatRequest(
            model="m", messages=(), max_tokens=50, metadata={"max_tokens": 30}
        )
        assert backend._build_params(request)["max_tokens"] == 50

    def test_metadata_max_tokens_overrides_instance_default(self):
        backend = self._backend(max_tokens=100)
        request = ChatRequest(model="m", messages=(), metadata={"max_tokens": 30})
        assert backend._build_params(request)["max_tokens"] == 30

    def test_instance_max_tokens_used_by_default(self):
        backend = self._backend(max_tokens=100)
        request = ChatRequest(model="m", messages=())
        assert backend._build_params(request)["max_tokens"] == 100

    def test_max_tokens_omitted_when_unset(self):
        backend = self._backend()
        request = ChatRequest(model="m", messages=())
        assert "max_tokens" not in backend._build_params(request)

    def test_extra_body_metadata_overrides_instance_default(self):
        backend = self._backend(extra_body={"enable_thinking": True})
        request = ChatRequest(
            model="m", messages=(), metadata={"extra_body": {"enable_thinking": False}}
        )
        assert backend._build_params(request)["extra_body"] == {"enable_thinking": False}

    def test_instance_extra_body_used_by_default(self):
        backend = self._backend(extra_body={"enable_thinking": True})
        request = ChatRequest(model="m", messages=())
        assert backend._build_params(request)["extra_body"] == {"enable_thinking": True}


@pytest.mark.asyncio
async def test_backend_client_does_not_double_wrap_tool_schemas():
    """get_schemas() returns full OpenAI-format dicts; the adapter must pass
    the inner function object through — not re-wrap it (production regression:
    provider 400 'body.tools.N.function.name Field required')."""
    captured: dict[str, Any] = {}

    class _CaptureBackend:
        name = "capture"
        supports_tool_calls = True
        supports_streaming = False

        async def chat(self, request: ChatRequest) -> ChatResponse:
            captured["request"] = request
            return ChatResponse(
                backend=self.name,
                model="capture",
                message=ChatMessage(role="assistant", content="ok"),
            )

        async def close(self) -> None:
            pass

    tool_schema = {
        "type": "function",
        "function": {
            "name": "parse_layout",
            "description": "Parse a document",
            "parameters": {"type": "object", "properties": {}},
        },
    }
    client = BackendModelClient(backend=_CaptureBackend(), model="capture")
    await client.generate([{"role": "user", "content": "hi"}], tools=[tool_schema])

    request = captured["request"]
    # The function payload must be the inner function object itself.
    assert request.tools[0].function == tool_schema["function"]

    # And the OpenAI wire params must stay single-nested.
    backend = OpenAIModelBackend(base_url="http://localhost", api_key="k")
    params = backend._build_params(request)
    assert params["tools"][0] == tool_schema
