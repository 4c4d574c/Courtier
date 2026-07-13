"""ModelClient — LLM abstraction layer."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from collections.abc import AsyncGenerator
from typing import Any, Protocol

from openai import AsyncOpenAI

from .streaming import extract_stream_delta, extract_reasoning, buffer_tool_call_delta

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
        # e.g. "use "$ref:parse_document:1" as input". Try to fix those.
        fixed = _fix_unescaped_ref_quotes(raw)
        if fixed != raw:
            try:
                result = json.loads(fixed)
            except json.JSONDecodeError:
                pass  # fall through to parse-error return
            else:
                if isinstance(result, dict):
                    logger.debug("Tool call arguments parsed after ref-quote fix")
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
                    return result
        logger.warning("Failed to parse tool call arguments: %s", raw[:200])
        return {"_parse_error": True, "raw": raw}
    if not isinstance(result, dict):
        logger.warning(
            "Parsed tool call arguments is not a dict (type=%s): %s",
            type(result).__name__,
            raw[:200],
        )
        return {"_parse_error": True, "raw": raw}
    return result


def _max_nesting_depth(raw: str) -> int:
    """Return the maximum brace/bracket nesting depth in *raw*.

    Used as a pre-check before ``ast.literal_eval`` to reject inputs that
    would cause excessive recursion.
    """
    depth = 0
    max_depth = 0
    for ch in raw:
        if ch in "{[":
            depth += 1
            if depth > max_depth:
                max_depth = depth
        elif ch in "}]":
            depth -= 1
    return max_depth


def _fix_unescaped_ref_quotes(raw: str) -> str:
    """Fix unescaped quotes around $ref values inside JSON string values.

    Models sometimes write::

        {"task": "use "$ref:parse_document:1" as input"}

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
    """Apply common JSON repairs to LLM-generated text.

    Handles: single-quoted strings → double-quoted, unquoted keys → quoted,
    trailing commas before closing brackets/braces.
    This is a safer alternative to ast.literal_eval for untrusted input.
    """
    repaired = raw.strip()
    # Replace single quotes with double quotes (careful with apostrophes)
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
                id=f"call_{len(tool_calls)}",
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
class ToolCall:
    """A tool call the model wants to execute."""

    id: str
    name: str
    arguments: dict[str, Any]


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


class OpenAIModelClient(ModelClient):
    """OpenAI 兼容适配器。

    Uses the OpenAI SDK for any OpenAI-compatible API.
    Configuration is passed explicitly — no global state.
    """

    def __init__(
        self,
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
        """Close the underlying AsyncOpenAI client."""
        await self._client.close()

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def temperature(self) -> float:
        return self._temperature

    async def generate(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        **kwargs: Any,
    ) -> ModelResponse:
        """Generate a response from the OpenAI-compatible API."""
        params: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": self._temperature,
        }
        if self._max_tokens is not None:
            params["max_tokens"] = self._max_tokens
        if self._frequency_penalty:
            params["frequency_penalty"] = self._frequency_penalty
        if self._presence_penalty:
            params["presence_penalty"] = self._presence_penalty
        if self._extra_body:
            params["extra_body"] = self._extra_body
        if tools:
            params["tools"] = tools

        params.update(kwargs)
        response = await self._client.chat.completions.create(**params)
        choice = response.choices[0]

        tool_calls = []
        if choice.message.tool_calls:
            for tc in choice.message.tool_calls:
                args = _parse_tool_arguments(tc.function.arguments)
                tool_calls.append(
                    ToolCall(id=tc.id, name=tc.function.name, arguments=args)
                )

        usage = None
        if response.usage:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }

        # Qwen3.5/DeepSeek-R1 use reasoning_content; some providers use reasoning.
        reasoning = (
            getattr(choice.message, "reasoning_content", None)
            or getattr(choice.message, "reasoning", None)
            or None
        )

        return _normalize_response(
            content=choice.message.content,
            tool_calls=tool_calls,
            reasoning=reasoning,
            finish_reason=choice.finish_reason or "stop",
            usage=usage,
            raw=response,
        )

    async def generate_stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[str, None]:
        """Generate streaming text tokens from the OpenAI-compatible API."""
        params: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": self._temperature,
            "stream": True,
        }
        if self._frequency_penalty:
            params["frequency_penalty"] = self._frequency_penalty
        if self._presence_penalty:
            params["presence_penalty"] = self._presence_penalty
        if tools:
            params["tools"] = tools

        params.update(kwargs)
        stream = await self._client.chat.completions.create(**params)
        async for chunk in stream:
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta and delta.content:
                yield delta.content

    async def generate_stream_full(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        on_token: Any = None,
        on_content_token: Any = None,
        **kwargs: Any,
    ) -> ModelResponse:
        """Generate via streaming API with separate callbacks for reasoning vs content.

        on_token: called for each reasoning token (chain-of-thought).
        on_content_token: called for each content token (final response text).

        Accumulates tool call deltas from the stream and returns a complete
        ModelResponse.
        """
        params: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": self._temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if self._max_tokens is not None:
            params["max_tokens"] = self._max_tokens
        if self._frequency_penalty:
            params["frequency_penalty"] = self._frequency_penalty
        if self._presence_penalty:
            params["presence_penalty"] = self._presence_penalty
        if self._extra_body:
            params["extra_body"] = self._extra_body
        if tools:
            params["tools"] = tools
        params.update(kwargs)

        stream = await self._client.chat.completions.create(**params)

        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_call_bufs: dict[int, dict[str, str]] = {}
        usage: dict[str, int] | None = None
        finish_reason: str = "stop"

        async for chunk in stream:
            if chunk.usage:
                usage = {
                    "prompt_tokens": chunk.usage.prompt_tokens or 0,
                    "completion_tokens": chunk.usage.completion_tokens or 0,
                    "total_tokens": chunk.usage.total_tokens or 0,
                }
            if chunk.choices and chunk.choices[0].finish_reason:
                finish_reason = chunk.choices[0].finish_reason
            delta = extract_stream_delta(chunk)
            if not delta:
                continue

            reasoning = extract_reasoning(delta)
            if reasoning:
                reasoning_parts.append(reasoning)
                if on_token:
                    await on_token(reasoning)

            if delta.content:
                content_parts.append(delta.content)
                if on_content_token:
                    await on_content_token(delta.content)

            buffer_tool_call_delta(
                delta, tool_call_bufs,
                name_key="name", arguments_key="arguments_str",
            )

        content = "".join(content_parts) if content_parts else None
        reasoning_content = "".join(reasoning_parts) if reasoning_parts else None

        tool_calls: list[ToolCall] = []
        for idx in sorted(tool_call_bufs.keys()):
            buf = tool_call_bufs[idx]
            args = _parse_tool_arguments(buf["arguments_str"])
            tool_calls.append(ToolCall(id=buf["id"], name=buf["name"], arguments=args))

        result = _normalize_response(
            content=content,
            tool_calls=tool_calls,
            reasoning=reasoning_content,
            finish_reason=finish_reason,
            usage=usage,
            raw=None,
        )

        # vLLM with Qwen models may return finish_reason="tool_calls" in
        # streaming mode but drop the actual tool call data (XML in content
        # or structured tool_calls deltas). Fall back to non-streaming.
        if tools and finish_reason == "tool_calls" and not result.tool_calls:
            logger.debug(
                "Streaming returned tool_calls finish but no tool calls; falling back to non-streaming."
            )
            try:
                return await self.generate(messages, tools=tools, **kwargs)
            except Exception:
                logger.exception(
                    "Non-streaming fallback also failed after streaming tool_calls drop"
                )
                raise

        return result


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
