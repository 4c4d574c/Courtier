"""Tests for ModelBackend, ModelRouter, and BackendModelClient."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

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
        fail: bool = False,
        latency_ms: float = 1.0,
    ) -> None:
        self._content = content
        self._tool_calls = tool_calls or []
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
            ),
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5),
            latency_ms=self._latency_ms,
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


class TestModelRouter:
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
