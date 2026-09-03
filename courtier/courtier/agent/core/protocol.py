"""Provider-agnostic protocol objects for LLM interactions.

These dataclasses decouple the agent loop from any specific model API shape.
Backends convert ``ChatRequest``/``ChatResponse`` to and from provider formats;
the agent loop works entirely with these normalized objects.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal

from .content_parts import (
    MessageContent,
    ensure_parts_allowed,
    parse_content,
    parts_to_internal,
)
from .tool_call import ToolCall


@dataclass(frozen=True)
class TokenUsage:
    """Token consumption for a single LLM call."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass(frozen=True)
class ChatMessage:
    """A single message in a normalized chat conversation.

    ``content`` is a plain string for system/assistant/tool messages; user
    messages may carry a ``TextPart``/``MediaPart`` list (internal logical
    form — media parts reference uploaded files, materialized to provider
    content parts at the backend boundary). See ``content_parts``.
    """

    role: Literal["system", "user", "assistant", "tool"]
    content: MessageContent = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    name: str | None = None
    # Model-internal chain-of-thought (Qwen reasoning_content / DeepSeek reasoning).
    # Never sent back to the provider; populated on assistant responses only.
    reasoning_content: str | None = None

    def __post_init__(self) -> None:
        ensure_parts_allowed(self.role, self.content)


def to_openai_dict(message: ChatMessage) -> dict[str, Any]:
    """Serialize a ``ChatMessage`` to the OpenAI wire format.

    This is the single canonical ChatMessage → wire conversion; all call
    sites (agent state, backends, conversation tree) delegate here.

    ``tool_calls`` arguments (held as dicts internally) are serialized to
    JSON strings with ``ensure_ascii=False`` so non-ASCII content stays
    readable. Internal-only fields (``reasoning_content``) are never emitted.

    Part-list content serializes to the internal logical form
    (``{"type": "text"}`` / ``{"type": "media"}`` dicts) — media
    materialization to provider content parts happens in the backend, not
    here.
    """
    d: dict[str, Any] = {"role": message.role}
    if message.content is not None:
        d["content"] = parts_to_internal(message.content)
    if message.tool_calls:
        d["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                },
            }
            for tc in message.tool_calls
        ]
    if message.tool_call_id is not None:
        d["tool_call_id"] = message.tool_call_id
    if message.name is not None:
        d["name"] = message.name
    return d


def from_openai_dict(d: dict[str, Any]) -> ChatMessage:
    """Parse an OpenAI wire dict into a ``ChatMessage``.

    This is the single canonical wire → ChatMessage conversion, inverse of
    ``to_openai_dict``. ``arguments`` semantics are unified here: string
    arguments are parsed back to dicts, and arguments that are already a dict
    are kept as-is.

    Parse failures follow the tolerant philosophy of
    ``model._parse_tool_arguments`` — they never raise. An unparseable string
    yields a ``{"_parse_error": True, "raw": <original string>}`` sentinel so
    the raw payload is preserved instead of crashing the caller. (The repair
    heuristics for malformed raw model output stay in
    ``_parse_tool_arguments``; wire dicts produced by ``to_openai_dict`` are
    always valid JSON and never need them.)
    """
    tool_calls = None
    if d.get("tool_calls"):
        tool_calls = [
            ToolCall(
                id=tc.get("id", ""),
                name=tc.get("function", {}).get("name", ""),
                arguments=_parse_wire_arguments(tc.get("function", {}).get("arguments")),
            )
            for tc in d["tool_calls"]
        ]
    return ChatMessage(
        role=d["role"],
        content=parse_content(d.get("content")),
        tool_calls=tool_calls,
        tool_call_id=d.get("tool_call_id"),
        name=d.get("name"),
    )


def _parse_wire_arguments(raw: Any) -> dict[str, Any]:
    """Parse a wire-format ``arguments`` value back into a dict.

    Dicts pass through unchanged; strings are parsed as JSON. Unparseable
    strings and non-dict JSON values yield the ``_parse_error`` sentinel
    (preserving the raw string); missing/empty arguments mean "no arguments".
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {"_parse_error": True, "raw": raw}
        if isinstance(parsed, dict):
            return parsed
        return {"_parse_error": True, "raw": raw}
    return {}


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
