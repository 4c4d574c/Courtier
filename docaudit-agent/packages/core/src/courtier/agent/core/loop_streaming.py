"""Streaming helpers for agent_loop."""

from __future__ import annotations

import logging
import unicodedata
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)


async def generate_with_streaming_fallback(
    *,
    model: Any,
    messages: list[dict],
    tools: list[dict] | None,
    on_token: Callable[[str], Awaitable[None]],
    on_content_token: Callable[[str], Awaitable[None]] | None = None,
) -> tuple[Any, bool]:
    """Generate a response, using true streaming when available.

    Reasoning tokens (chain-of-thought) go to on_token.
    Final content/response tokens go to on_content_token.

    Returns (ModelResponse, tokens_streamed: bool).
    """
    stream_method = getattr(model, "generate_stream_full", None)
    if stream_method is not None:
        response = await stream_method(
            messages,
            tools=tools,
            on_token=on_token,
            on_content_token=on_content_token,
        )
    else:
        response = await model.generate(messages, tools=tools)
        _consecutive_callback_failures = 0
        _MAX_CONSECUTIVE_FAILURES = 10
        if response.reasoning_content:
            for token in _split_tokens(response.reasoning_content):
                try:
                    await on_token(token)
                    _consecutive_callback_failures = 0
                except Exception:
                    _consecutive_callback_failures += 1
                    logger.warning(
                        "Token callback on_token failed for token: %r", token
                    )
                    if _consecutive_callback_failures >= _MAX_CONSECUTIVE_FAILURES:
                        logger.error(
                            "Token callback on_token failed %d consecutive times; stopping",
                            _consecutive_callback_failures,
                        )
                        break
        if response.content and on_content_token:
            _consecutive_callback_failures = 0
            for token in _split_tokens(response.content):
                try:
                    await on_content_token(token)
                    _consecutive_callback_failures = 0
                except Exception:
                    _consecutive_callback_failures += 1
                    logger.warning(
                        "Token callback on_content_token failed for token: %r", token
                    )
                    if _consecutive_callback_failures >= _MAX_CONSECUTIVE_FAILURES:
                        logger.error(
                            "Token callback on_content_token failed %d consecutive times; stopping",
                            _consecutive_callback_failures,
                        )
                        break

    tokens_streamed = bool(response.content and not response.tool_calls)
    return response, tokens_streamed


def _split_tokens(text: str):
    """Split text into display tokens for simulated streaming."""
    buf = ""
    for ch in text:
        if ch in (" ", "\n"):
            if buf:
                yield buf
                buf = ""
            yield ch
        elif unicodedata.east_asian_width(ch) in ("W", "F"):
            if buf:
                yield buf
                buf = ""
            yield ch
        else:
            buf += ch
    if buf:
        yield buf
