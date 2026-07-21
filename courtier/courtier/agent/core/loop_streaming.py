"""Streaming helpers for agent_loop."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any


async def generate_with_streaming_fallback(
    *,
    model: Any,
    messages: list[dict],
    tools: list[dict] | None,
    on_token: Callable[[str], Awaitable[None]],
    on_content_token: Callable[[str], Awaitable[None]] | None = None,
) -> tuple[Any, bool]:
    """Generate a response via the model's streaming API.

    Reasoning tokens (chain-of-thought) go to on_token.
    Final content/response tokens go to on_content_token.

    Returns (ModelResponse, tokens_streamed: bool).
    """
    response = await model.generate_stream_full(
        messages,
        tools=tools,
        on_token=on_token,
        on_content_token=on_content_token,
    )
    tokens_streamed = bool(response.content and not response.tool_calls)
    return response, tokens_streamed
