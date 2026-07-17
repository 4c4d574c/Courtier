"""OpenAI-compatible ModelBackend implementation."""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from typing import Any

from openai import AsyncOpenAI

from ..model import _normalize_response, _parse_tool_arguments
from ..protocol import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    TokenChunk,
    TokenUsage,
    ToolCall,
)
from ..streaming import buffer_tool_call_delta, extract_reasoning, extract_stream_delta

logger = logging.getLogger(__name__)


class OpenAIModelBackend:
    """Backend for any OpenAI-compatible chat completions API."""

    name = "openai"
    supports_tool_calls = True
    supports_streaming = True

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str = "qwen3.5-27b",
        temperature: float = 0.6,
        timeout: float = 180.0,
        max_tokens: int | None = None,
        extra_body: dict[str, Any] | None = None,
        frequency_penalty: float = 0.0,
        presence_penalty: float = 0.0,
    ) -> None:
        self._client = AsyncOpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=timeout,
        )
        self._model = model
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._extra_body = extra_body
        self._frequency_penalty = frequency_penalty
        self._presence_penalty = presence_penalty

    async def close(self) -> None:
        await self._client.close()

    def _build_params(self, request: ChatRequest) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": request.model or self._model,
            "messages": [_message_to_openai(m) for m in request.messages],
            "temperature": request.temperature,
        }
        if request.metadata:
            max_tokens = request.metadata.get("max_tokens", self._max_tokens)
        else:
            max_tokens = self._max_tokens
        if max_tokens is not None:
            params["max_tokens"] = max_tokens
        if self._frequency_penalty:
            params["frequency_penalty"] = self._frequency_penalty
        if self._presence_penalty:
            params["presence_penalty"] = self._presence_penalty
        if self._extra_body:
            params["extra_body"] = self._extra_body
        if request.tools:
            params["tools"] = [_tool_schema_to_openai(t) for t in request.tools]
        return params

    async def chat(self, request: ChatRequest) -> ChatResponse:
        start = time.perf_counter()
        params = self._build_params(request)
        response = await self._client.chat.completions.create(**params)
        latency_ms = (time.perf_counter() - start) * 1000

        choice = response.choices[0]
        tool_calls: list[ToolCall] = []
        if choice.message.tool_calls:
            for tc in choice.message.tool_calls:
                args = _parse_tool_arguments(tc.function.arguments)
                tool_calls.append(
                    ToolCall(id=tc.id, name=tc.function.name, arguments=args)
                )

        usage = _openai_usage_to_normalized(response.usage)
        reasoning = (
            getattr(choice.message, "reasoning_content", None)
            or getattr(choice.message, "reasoning", None)
            or None
        )

        normalized = _normalize_response(
            content=choice.message.content,
            tool_calls=tool_calls,
            reasoning=reasoning,
            finish_reason=choice.finish_reason or "stop",
            usage={
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "total_tokens": usage.total_tokens,
            },
            raw=response,
        )

        return ChatResponse(
            backend=self.name,
            model=self._model,
            message=ChatMessage(
                role="assistant",
                content=normalized.content,
                tool_calls=normalized.tool_calls or None,
            ),
            usage=usage,
            finish_reason=normalized.finish_reason,
            latency_ms=latency_ms,
            raw=normalized.raw,
        )

    async def stream(
        self, request: ChatRequest
    ) -> AsyncIterator[TokenChunk | ChatResponse]:
        start = time.perf_counter()
        params = self._build_params(request)
        params["stream"] = True
        params["stream_options"] = {"include_usage": True}

        stream = await self._client.chat.completions.create(**params)
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_call_bufs: dict[int, dict[str, str]] = {}
        usage: TokenUsage | None = None
        finish_reason: str = "stop"

        async for chunk in stream:
            if chunk.usage:
                usage = _openai_usage_to_normalized(chunk.usage)
            if chunk.choices and chunk.choices[0].finish_reason:
                finish_reason = chunk.choices[0].finish_reason

            delta = extract_stream_delta(chunk)
            if not delta:
                continue

            reasoning = extract_reasoning(delta)
            if reasoning:
                reasoning_parts.append(reasoning)
                yield TokenChunk(text=reasoning, kind="reasoning")

            if delta.content:
                content_parts.append(delta.content)
                yield TokenChunk(text=delta.content, kind="content")

            buffer_tool_call_delta(
                delta,
                tool_call_bufs,
                name_key="name",
                arguments_key="arguments_str",
            )

        latency_ms = (time.perf_counter() - start) * 1000
        content = "".join(content_parts) if content_parts else None
        reasoning_content = "".join(reasoning_parts) if reasoning_parts else None

        tool_calls: list[ToolCall] = []
        for idx in sorted(tool_call_bufs.keys()):
            buf = tool_call_bufs[idx]
            args = _parse_tool_arguments(buf["arguments_str"])
            tool_calls.append(ToolCall(id=buf["id"], name=buf["name"], arguments=args))

        normalized = _normalize_response(
            content=content,
            tool_calls=tool_calls,
            reasoning=reasoning_content,
            finish_reason=finish_reason,
            usage={
                "prompt_tokens": usage.prompt_tokens if usage else 0,
                "completion_tokens": usage.completion_tokens if usage else 0,
                "total_tokens": usage.total_tokens if usage else 0,
            },
            raw=None,
        )

        yield ChatResponse(
            backend=self.name,
            model=self._model,
            message=ChatMessage(
                role="assistant",
                content=normalized.content,
                tool_calls=normalized.tool_calls or None,
            ),
            usage=usage,
            finish_reason=normalized.finish_reason,
            latency_ms=latency_ms,
            raw=normalized.raw,
        )


def _message_to_openai(message: ChatMessage) -> dict[str, Any]:
    d: dict[str, Any] = {"role": message.role}
    if message.content is not None:
        d["content"] = message.content
    if message.tool_calls:
        d["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": __import__("json").dumps(tc.arguments, ensure_ascii=False),
                },
            }
            for tc in message.tool_calls
        ]
    if message.tool_call_id is not None:
        d["tool_call_id"] = message.tool_call_id
    if message.name is not None:
        d["name"] = message.name
    return d


def _tool_schema_to_openai(schema: Any) -> dict[str, Any]:
    return {"type": schema.type, "function": dict(schema.function)}


def _openai_usage_to_normalized(usage: Any) -> TokenUsage:
    if usage is None:
        return TokenUsage()
    return TokenUsage(
        prompt_tokens=usage.prompt_tokens or 0,
        completion_tokens=usage.completion_tokens or 0,
        total_tokens=usage.total_tokens or 0,
    )
