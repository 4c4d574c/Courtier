"""Provider-agnostic protocol objects for LLM interactions.

These dataclasses decouple the agent loop from any specific model API shape.
Backends convert ``ChatRequest``/``ChatResponse`` to and from provider formats;
the agent loop works entirely with these normalized objects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from .tool_call import ToolCall


@dataclass(frozen=True)
class TokenUsage:
    """Token consumption for a single LLM call."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass(frozen=True)
class ChatMessage:
    """A single message in a normalized chat conversation."""

    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    name: str | None = None


@dataclass(frozen=True)
class ToolSchema:
    """Normalized tool schema exposed to the model."""

    type: str = "function"
    function: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ChatRequest:
    """Normalized request to any LLM backend.

    Attributes:
        model: Backend-specific model identifier.
        messages: Conversation history in normalized form.
        temperature: Sampling temperature.
        max_tokens: Maximum tokens to generate, if enforced.
        tools: Tool schemas the model may invoke.
        metadata: Backend-specific hints (e.g. extra_body, top_p).
    """

    model: str
    messages: tuple[ChatMessage, ...]
    temperature: float = 0.7
    max_tokens: int | None = None
    tools: list[ToolSchema] | None = None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class ChatResponse:
    """Normalized response from any LLM backend.

    Attributes:
        backend: Identifier of the backend that produced the response.
        model: Model identifier as reported by the backend.
        message: The assistant message (content and/or tool calls).
        usage: Token usage, if reported.
        finish_reason: Provider finish reason.
        latency_ms: Wall-clock time for the call.
        raw: Optional provider-specific raw response for debugging.
    """

    backend: str
    model: str
    message: ChatMessage
    usage: TokenUsage | None = None
    finish_reason: str = "stop"
    latency_ms: float = 0.0
    raw: Any = None


@dataclass(frozen=True)
class TokenChunk:
    """A single streamed token from a backend."""

    text: str
    kind: Literal["reasoning", "content"] = "content"
