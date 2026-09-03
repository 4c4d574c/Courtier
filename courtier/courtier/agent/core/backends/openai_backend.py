"""OpenAI-compatible ModelBackend implementation."""

from __future__ import annotations

import base64
import io
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from openai import AsyncOpenAI

from ..content_parts import KIND_TO_MODALITY
from ..model import _normalize_response, _parse_tool_arguments
from ..protocol import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    TokenChunk,
    TokenUsage,
    ToolCall,
    to_openai_dict,
)
from ..streaming import buffer_tool_call_delta, extract_reasoning, extract_stream_delta

logger = logging.getLogger(__name__)

# vLLM/SGLang 的 input_audio format token → 按 MIME 子类型映射。
_AUDIO_FORMAT_BY_MIME: dict[str, str] = {
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/vnd.wave": "wav",
    "audio/wave": "wav",
    "audio/mpeg": "mp3",
    "audio/flac": "flac",
    "audio/x-flac": "flac",
    "audio/mp4": "m4a",
    "audio/x-m4a": "m4a",
}

# MediaResolver(file_id, kind) -> (bytes, mime)；由 build_model_client 注入
# （FileStore 读取），核心层不直接触达上传目录。
MediaResolver = Callable[[str, str], Awaitable[tuple[bytes, str]]]


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
        media_resolver: MediaResolver | None = None,
        declared_modalities: tuple[str, ...] = (),
        image_max_edge: int | None = None,
        image_jpeg_quality: int = 85,
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
        self._media_resolver = media_resolver
        self._declared_modalities = frozenset(declared_modalities)
        self._image_max_edge = image_max_edge
        self._image_jpeg_quality = image_jpeg_quality

    async def close(self) -> None:
        await self._client.close()

    async def _materialize_messages(self, messages: tuple[ChatMessage, ...]) -> list[dict[str, Any]]:
        """Convert messages to wire dicts, materializing media parts.

        Media parts become provider content parts (base64 data URIs /
        input_audio) at this boundary only. A part whose modality the model
        does not declare — or whose bytes cannot be resolved — degrades to a
        readable text placeholder (historical-media rule; new attachments
        are gated before the run starts).
        """
        wire: list[dict[str, Any]] = []
        for message in messages:
            d = to_openai_dict(message)
            content = d.get("content")
            if not isinstance(content, list):
                wire.append(d)
                continue
            parts: list[dict[str, Any]] = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "media":
                    parts.append(await self._materialize_media(item))
                else:
                    parts.append(item)
            d["content"] = parts
            wire.append(d)
        return wire

    async def _materialize_media(self, item: dict[str, Any]) -> dict[str, Any]:
        kind = item.get("kind", "")
        name = item.get("name") or item.get("file_id", "")
        label = {"image": "图片", "audio": "音频", "video": "视频"}.get(kind, kind)
        placeholder = {"type": "text", "text": f"[附件: {name}（{label}）已省略]"}

        if KIND_TO_MODALITY.get(kind, kind) not in self._declared_modalities:
            logger.info(
                "media part %s (%s) dropped: model %s lacks %s",
                item.get("file_id"),
                kind,
                self._model,
                KIND_TO_MODALITY.get(kind, kind),
            )
            return placeholder
        if self._media_resolver is None:
            return placeholder
        try:
            data, mime = await self._media_resolver(item["file_id"], kind)
        except Exception:
            logger.warning(
                "media part %s could not be resolved; degrading to placeholder",
                item.get("file_id"),
                exc_info=True,
            )
            return placeholder

        b64 = base64.b64encode(data).decode()
        if kind == "image":
            data, mime = _normalize_image_bytes(
                data, mime, self._image_max_edge, self._image_jpeg_quality
            )
            b64 = base64.b64encode(data).decode()
            return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}
        if kind == "audio":
            fmt = _AUDIO_FORMAT_BY_MIME.get(mime, "wav")
            return {"type": "input_audio", "input_audio": {"data": b64, "format": fmt}}
        if kind == "video":
            return {"type": "video_url", "video_url": {"url": f"data:{mime};base64,{b64}"}}
        return placeholder

    def _build_params(
        self,
        request: ChatRequest,
        messages: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Build request params; *messages* overrides the wire message list.

        Callers that may carry media parts must first materialize via
        :meth:`_materialize_messages` and pass the result here — base64
        bytes cannot be produced synchronously.
        """
        params: dict[str, Any] = {
            "model": request.model or self._model,
            "messages": (
                messages if messages is not None else [_message_to_openai(m) for m in request.messages]
            ),
            "temperature": request.temperature,
        }
        return self._finish_params(request, params)

    def _finish_params(self, request: ChatRequest, params: dict[str, Any]) -> dict[str, Any]:
        # max_tokens precedence: per-request field → metadata hint → instance default.
        max_tokens = request.max_tokens
        if max_tokens is None and request.metadata:
            max_tokens = request.metadata.get("max_tokens")
        if max_tokens is None:
            max_tokens = self._max_tokens
        if max_tokens is not None:
            params["max_tokens"] = max_tokens
        if self._frequency_penalty:
            params["frequency_penalty"] = self._frequency_penalty
        if self._presence_penalty:
            params["presence_penalty"] = self._presence_penalty
        # extra_body: instance default, overridable per request via metadata.
        extra_body = self._extra_body
        if request.metadata and request.metadata.get("extra_body") is not None:
            extra_body = request.metadata["extra_body"]
        if extra_body:
            params["extra_body"] = extra_body
        if request.tools:
            params["tools"] = [_tool_schema_to_openai(t) for t in request.tools]
        return params

    async def chat(self, request: ChatRequest) -> ChatResponse:
        start = time.perf_counter()
        messages = await self._materialize_messages(request.messages)
        params = self._build_params(request, messages)
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
                reasoning_content=normalized.reasoning_content,
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
        messages = await self._materialize_messages(request.messages)
        params = self._build_params(request, messages)
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

            buffer_tool_call_delta(delta, tool_call_bufs)

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
                reasoning_content=normalized.reasoning_content,
            ),
            usage=usage,
            finish_reason=normalized.finish_reason,
            latency_ms=latency_ms,
            raw=normalized.raw,
        )


def _message_to_openai(message: ChatMessage) -> dict[str, Any]:
    """Thin wrapper over ``protocol.to_openai_dict`` (single canonical conversion)."""
    return to_openai_dict(message)


def _normalize_image_bytes(
    data: bytes,
    mime: str,
    max_edge: int | None,
    jpeg_quality: int,
) -> tuple[bytes, str]:
    """Normalize an image before inlining: cap the long edge, re-encode big frames.

    Small PNGs (within the edge cap) stay byte-identical — transparency is
    preserved and no generation loss is introduced. Oversized or non-PNG
    raster frames are re-encoded as JPEG. Pillow is optional at runtime;
    when it is missing the original bytes pass through unchanged.
    """
    if max_edge is None:
        return data, mime
    try:
        from PIL import Image
    except ImportError:
        return data, mime

    try:
        with Image.open(io.BytesIO(data)) as img:
            width, height = img.size
            longest = max(width, height)
            needs_resize = longest > max_edge
            passthrough_mime = mime in ("image/png", "image/jpeg", "image/webp")
            if passthrough_mime and not needs_resize:
                return data, mime
            if needs_resize:
                scale = max_edge / float(longest)
                img = img.resize(
                    (max(1, round(width * scale)), max(1, round(height * scale))),
                    Image.LANCZOS,
                )
            if (mime == "image/png" or mime == "image/gif") and (
                img.mode in ("RGBA", "LA", "P")
            ):
                # transparency survives only in PNG — keep the container
                buf = io.BytesIO()
                img.save(buf, format="PNG", optimize=True)
                return buf.getvalue(), "image/png"
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="JPEG", quality=jpeg_quality)
            return buf.getvalue(), "image/jpeg"
    except Exception:
        logger.warning("image normalization failed; inlining original bytes", exc_info=True)
        return data, mime


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
