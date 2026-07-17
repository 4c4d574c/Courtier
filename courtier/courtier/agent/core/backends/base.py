"""ModelBackend protocol — provider-agnostic LLM interface.

A ``ModelBackend`` consumes normalized ``ChatRequest`` objects and returns
normalized ``ChatResponse`` objects. The agent loop and router operate on
this abstraction; concrete backends translate to OpenAI, Anthropic, local
vLLM, or other provider APIs.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from ..protocol import ChatRequest, ChatResponse, TokenChunk


class ModelBackend(Protocol):
    """Provider-agnostic interface for any LLM backend."""

    name: str
    supports_tool_calls: bool
    supports_streaming: bool

    async def chat(self, request: ChatRequest) -> ChatResponse:
        """Generate a complete response for *request*."""
        ...

    def stream(
        self, request: ChatRequest
    ) -> AsyncIterator[TokenChunk | ChatResponse]:
        """Stream tokens and finish with the aggregated ``ChatResponse``.

        The iterator yields ``TokenChunk`` instances while generation is in
        progress and a final ``ChatResponse`` containing usage and finish
        metadata. Consumers that do not need streaming can call ``chat()``.
        """
        ...
