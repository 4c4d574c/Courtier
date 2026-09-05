"""ModelClient — LLM abstraction layer."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from ..telemetry.metrics import record_stream_tool_calls_lost, record_tool_arg_repair
from .tool_call import ToolCall

if TYPE_CHECKING:
    from .protocol import ChatMessage

logger = logging.getLogger(__name__)


def _parse_tool_arguments(raw: str) -> dict[str, Any]:
    """Parse tool call arguments with fallback for non-standard JSON.

    Primary: standard JSON (double quotes).
    Pre-process: quote $ref:tool:N values so the model can pass ref IDs
    as instructed by ContextManager.get_ref_instructions().
    Fallback: Python literal eval for single-quoted dicts that some
    models (e.g. Qwen via certain API gateways) occasionally emit.
    """
    # Pre-process: quote bare $ref:tool_name:N values so they become valid JSON strings.
    raw = _REF_ARG_PATTERN.sub(r'"\1"', raw)
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        # Some models emit $ref values with quotes inside a larger string value,
        # e.g. "use "$ref:parse_layout:1" as input". Try to fix those.
        fixed = _fix_unescaped_ref_quotes(raw)
        if fixed != raw:
            try:
                result = json.loads(fixed)
            except json.JSONDecodeError:
                pass  # fall through to parse-error return
            else:
                if isinstance(result, dict):
                    logger.debug("Tool call arguments parsed after ref-quote fix")
                    record_tool_arg_repair("ref_quote_fix")
                    return result
        # Attempt common JSON repairs before giving up: single quotes → double quotes,
        # trailing commas, unquoted keys.  This replaces the ast.literal_eval fallback
        # which was never designed as a security boundary for untrusted LLM output.
        repaired = _repair_json(raw)
        if repaired != raw:
            try:
                result = json.loads(repaired)
            except json.JSONDecodeError:
                pass
            else:
                if isinstance(result, dict):
                    logger.debug("Tool call arguments parsed after JSON repair")
                    record_tool_arg_repair("json_repair")
                    return result
        logger.warning("Failed to parse tool call arguments: %s", raw[:200])
        record_tool_arg_repair("parse_error")
        return {"_parse_error": True, "raw": raw}
    if not isinstance(result, dict):
        logger.warning(
            "Parsed tool call arguments is not a dict (type=%s): %s",
            type(result).__name__,
            raw[:200],
        )
        record_tool_arg_repair("parse_error")
        return {"_parse_error": True, "raw": raw}
    return result


def _fix_unescaped_ref_quotes(raw: str) -> str:
    """Fix unescaped quotes around $ref values inside JSON string values.

    Models sometimes write::

        {"task": "use "$ref:parse_layout:1" as input"}

    where the inner quotes around $ref are not escaped. We detect such
    patterns by checking whether the surrounding quotes sit inside a larger
    JSON string (odd number of unescaped quotes preceding the opening quote).
    """
    chars = list(raw)
    offset = 0
    for match in re.finditer(r'\$ref:[\w\-]+:\d+', raw):
        start, end = match.span()
        # Adjust for previous replacements
        start += offset
        end += offset
        if start > 0 and end < len(chars) and chars[start - 1] == '"' and chars[end] == '"':
            prefix = ''.join(chars[:start - 1])
            # Count unescaped double quotes in prefix
            unescaped_quotes = len(re.findall(r'(?<!\\)"', prefix))
            if unescaped_quotes % 2 == 1:  # Inside a larger string value
                chars[start - 1] = '\\"'
                chars[end] = '\\"'
                offset += 2  # Each replacement adds one char (" -> \")
    return ''.join(chars)


def _repair_json(raw: str) -> str:
    """Apply conservative JSON repairs to LLM-generated text.

    Handles: single-quoted strings → double-quoted, unquoted keys → quoted,
    trailing commas before closing brackets/braces.

    The single-quote replacement runs only when the payload contains no
    double quotes at all (pure single-quote style) — otherwise apostrophes
    and quoted segments inside string values would be corrupted. Any repair
    that still fails ``json.loads`` falls back to the ``_parse_error``
    sentinel, so a bad repair never silently alters valid content.
    """
    repaired = raw.strip()
    if '"' not in repaired:
        # Pure single-quote style is unambiguous to convert.
        repaired = re.sub(r"(?<!\\)'", '"', repaired)
    # Quote unquoted keys: word followed by colon (not inside strings)
    repaired = re.sub(r'([{,]\s*)(\w+)(\s*:)', r'\1"\2"\3', repaired)
    # Remove trailing commas before ] or }
    repaired = re.sub(r',(\s*[}\]])', r'\1', repaired)
    return repaired


# Matches $ref:word_chars:digits that is NOT already inside quotes.
# Supports lowercase, uppercase, and hyphens in tool names.
_REF_ARG_PATTERN = re.compile(r'(?<!")(\$ref:[\w\-]+:\d+)(?!")')

# Some vLLM-served Qwen models emit tool calls as XML text in content
# instead of the standard OpenAI tool_calls array. Example:
#   <tool_call>
#   <function=get_weather>
#   <parameter=city>
#   北京
#   </parameter>
#   </function>
#   </tool_call>
_TOOL_CALL_XML_PATTERN = re.compile(r"<tool_call>(.*?)</tool_call>", re.DOTALL)
_FUNC_NAME_PATTERN = re.compile(r"<function=([^>]+)>")
_PARAM_PATTERN = re.compile(r"<parameter=([^>]+)>\s*(.*?)\s*</parameter>", re.DOTALL)


def _parse_xml_tool_calls(content: str) -> tuple[list[ToolCall], str]:
    """Parse <tool_call> XML blocks from content, return (tool_calls, cleaned_content).

    Some vLLM endpoints return tool calls embedded as XML in the text content
    rather than in the standard tool_calls array. This extracts them and
    removes the XML blocks from the content string.
    """
    tool_calls: list[ToolCall] = []
    cleaned = content

    for block_match in _TOOL_CALL_XML_PATTERN.finditer(content):
        block = block_match.group(1)
        func_match = _FUNC_NAME_PATTERN.search(block)
        if not func_match:
            continue
        func_name = func_match.group(1).strip()

        params: dict[str, Any] = {}
        for param_match in _PARAM_PATTERN.finditer(block):
            name = param_match.group(1).strip()
            value = param_match.group(2).strip()
            params[name] = value

        tool_calls.append(
            ToolCall(
                # Unique per call so IDs do not repeat across turns.
                id=f"call_{uuid.uuid4().hex[:8]}_{len(tool_calls)}",
                name=func_name,
                arguments=params,
            )
        )

    cleaned = _TOOL_CALL_XML_PATTERN.sub("", cleaned).strip()

    return tool_calls, cleaned


# NOTE: _normalize_response is called with tool_calls=[] from generate()
# and generate_stream_full().  If a model returns both tool_calls in the array
# AND XML <tool_call> blocks in content, the XML blocks are removed and the
# cleaned content may be empty.  This edge case is unlikely with current
# models but worth noting.
def _normalize_response(
    content: str | None,
    tool_calls: list[ToolCall],
    reasoning: str | None,
    finish_reason: str,
    usage: dict[str, int] | None,
    raw: Any,
) -> ModelResponse:
    """Normalize a response, applying XML tool call fallback if needed."""
    if not tool_calls and content and "<tool_call>" in content:
        parsed_calls, cleaned_content = _parse_xml_tool_calls(content)
        if parsed_calls:
            logger.debug(
                "Parsed %d tool call(s) from XML in content", len(parsed_calls)
            )
            record_tool_arg_repair("xml_fallback")
            return ModelResponse(
                content=cleaned_content or None,
                reasoning_content=reasoning,
                tool_calls=parsed_calls,
                finish_reason=finish_reason,
                usage=usage,
                raw=raw,
            )
    return ModelResponse(
        content=content,
        reasoning_content=reasoning,
        tool_calls=tool_calls,
        finish_reason=finish_reason,
        usage=usage,
        raw=raw,
    )


@dataclass(frozen=True)
class ModelResponse:
    """Normalized model response across providers.

    reasoning_content: model-internal chain-of-thought (e.g. Qwen3.5 reasoning_content,
    DeepSeek R1 reasoning). Streamed as "thinking" display tokens.
    """

    content: str | None
    reasoning_content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: dict[str, int] | None = None
    raw: Any = None


class ModelClient(Protocol):
    """LLM 客户端抽象 — 隔离具体模型提供商。"""

    @property
    def model_name(self) -> str: ...

    async def close(self) -> None:
        """Close the underlying API client and release resources."""
        ...

    async def generate(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        **kwargs: Any,
    ) -> ModelResponse:
        """Generate a response (text or tool calls) from the model."""
        ...

    async def generate_stream_full(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        on_token: Callable[[str], Awaitable[None]] | None = None,
        on_content_token: Callable[[str], Awaitable[None]] | None = None,
        **kwargs: Any,
    ) -> ModelResponse:
        """Generate via streaming, invoking callbacks for reasoning/content tokens."""
        ...


class BackendModelClient(ModelClient):
    """Adapter exposing a ``ModelBackend`` through the legacy ``ModelClient`` interface.

    This lets the rest of the codebase keep using ``ModelClient`` while the
    underlying backend benefits from the normalized ``ChatRequest``/``ChatResponse``
    protocol, routing, and fallback introduced in Phase 2.
    """

    def __init__(
        self,
        backend: Any,
        model: str = "unknown",
        temperature: float = 0.7,
    ) -> None:
        self._backend = backend
        self._model = model
        self._temperature = temperature

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def temperature(self) -> float:
        return self._temperature

    async def close(self) -> None:
        close = getattr(self._backend, "close", None)
        if close is not None:
            await close()

    async def generate(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        **kwargs: Any,
    ) -> ModelResponse:
        from .protocol import ChatRequest, ToolSchema

        request = ChatRequest(
            model=self._model,
            messages=tuple(_openai_message_to_chat(m) for m in messages),
            temperature=self._temperature,
            tools=(
                # get_schemas() returns full OpenAI-format dicts — unwrap to
                # the inner function object instead of re-wrapping it
                # (double-wrapped tools are rejected with 400 by providers).
                [ToolSchema(function=t.get("function", t)) for t in tools]
                if tools
                else None
            ),
            metadata=kwargs or None,
        )
        response = await self._backend.chat(request)
        message = response.message
        return ModelResponse(
            content=message.content,
            reasoning_content=message.reasoning_content,
            tool_calls=message.tool_calls or [],
            finish_reason=response.finish_reason,
            usage={
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }
            if response.usage
            else None,
            raw=response.raw,
        )

    async def generate_stream_full(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        on_token: Any = None,
        on_content_token: Any = None,
        **kwargs: Any,
    ) -> ModelResponse:
        from .protocol import ChatRequest, TokenChunk, ToolSchema

        request = ChatRequest(
            model=self._model,
            messages=tuple(_openai_message_to_chat(m) for m in messages),
            temperature=self._temperature,
            tools=(
                # get_schemas() returns full OpenAI-format dicts — unwrap to
                # the inner function object instead of re-wrapping it
                # (double-wrapped tools are rejected with 400 by providers).
                [ToolSchema(function=t.get("function", t)) for t in tools]
                if tools
                else None
            ),
            metadata=kwargs or None,
        )

        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        finish_reason = "stop"
        usage = None
        raw = None

        stream_iter = self._backend.stream(request)
        # Some backends do not support streaming; fall back to chat().
        try:
            if asyncio.iscoroutine(stream_iter):
                stream_iter = await stream_iter
        except NotImplementedError:
            return await self.generate(messages, tools=tools, **kwargs)

        async for chunk in stream_iter:
            if isinstance(chunk, TokenChunk):
                if chunk.kind == "reasoning":
                    reasoning_parts.append(chunk.text)
                    if on_token:
                        await on_token(chunk.text)
                elif chunk.kind == "content":
                    content_parts.append(chunk.text)
                    if on_content_token:
                        await on_content_token(chunk.text)
            else:
                finish_reason = chunk.finish_reason
                usage = chunk.usage
                tool_calls = chunk.message.tool_calls or []
                raw = chunk.raw

        result = ModelResponse(
            content="".join(content_parts) if content_parts else None,
            reasoning_content="".join(reasoning_parts) if reasoning_parts else None,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage={
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "total_tokens": usage.total_tokens,
            }
            if usage
            else None,
            raw=raw,
        )

        # vLLM with Qwen models may return finish_reason="tool_calls" in
        # streaming mode but drop the actual tool call data (XML in content
        # or structured tool_calls deltas). Fall back to non-streaming.
        if tools and finish_reason == "tool_calls" and not result.tool_calls:
            logger.debug(
                "Streaming returned tool_calls finish but no tool calls; "
                "falling back to non-streaming."
            )
            record_stream_tool_calls_lost(self._model)
            try:
                fallback = await self.generate(messages, tools=tools, **kwargs)
            except Exception:
                logger.exception(
                    "Non-streaming fallback also failed after streaming tool_calls drop"
                )
                raise
            # The streamed content of the dropped attempt already reached the
            # UI: reset its buffers, then stream the fallback content through
            # the same callbacks so the real conclusion renders in place of
            # the ghost text.
            if content_parts and on_content_token:
                await on_content_token(STREAM_RESET_MARKER)
            if fallback.content:
                await on_content_token(fallback.content)
            return fallback

        return result


#: Sentinel streamed through on_content_token to tell the frontend to reset
#: the current step's buffers (the streamed attempt was dropped and replaced).
STREAM_RESET_MARKER = "\u0000stream-reset\u0000"

def _openai_message_to_chat(message: dict) -> "ChatMessage":
    """Convert an OpenAI wire dict to a ``ChatMessage``.

    Thin wrapper over ``protocol.from_openai_dict`` (single canonical
    conversion). Note the unified ``arguments`` semantics: string arguments
    are now parsed back to dicts (previously passed through as raw strings,
    which could double-encode when re-serialized by the backend).
    """
    from .protocol import from_openai_dict

    return from_openai_dict(message)


class MockModelClient(ModelClient):
    """A deterministic model client for testing.

    Returns a predetermined response — no LLM call needed.
    """

    def __init__(self, tool_calls: list[ToolCall] | None = None) -> None:
        self._tool_calls = tool_calls or []
        self._call_count = 0

    @property
    def model_name(self) -> str:
        return "mock"

    @property
    def temperature(self) -> float:
        return 0.0

    async def close(self) -> None:
        """No-op: mock client has no resources to release."""
        return

    async def generate(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        **kwargs: Any,
    ) -> ModelResponse:
        self._call_count += 1
        if self._call_count == 1 and self._tool_calls:
            return ModelResponse(
                content=None,
                tool_calls=list(self._tool_calls),
            )
        return ModelResponse(content="Done.", tool_calls=[])

    async def generate_stream_full(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        on_token: Any = None,
        on_content_token: Any = None,
        **kwargs: Any,
    ) -> ModelResponse:
        """Mock streaming — delegates to generate(), sends content via callbacks."""
        response = await self.generate(messages, tools=tools, **kwargs)
        text = response.content or ""
        if on_token and text:
            for char in text:
                await on_token(char)
        if on_content_token and text:
            await on_content_token(text)
        return response
