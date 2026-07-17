"""ModelBackend router with fallback and basic routing strategies."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from ...telemetry.metrics import record_model_router_fallback
from ..event_bus import EventBus
from ..events import AgentEvent
from ..protocol import ChatRequest, ChatResponse, TokenChunk
from .base import ModelBackend

logger = logging.getLogger(__name__)


class ModelUnavailable(Exception):
    """Raised when a model backend cannot produce a response."""

    def __init__(self, backend: str, reason: str) -> None:
        self.backend = backend
        self.reason = reason
        super().__init__(f"Backend {backend} unavailable: {reason}")


class NoBackendAvailable(Exception):
    """Raised when all configured backends fail."""

    def __init__(self, request: ChatRequest) -> None:
        self.request = request
        super().__init__(f"No backend available for model {request.model}")


@dataclass(frozen=True)
class RoutingStrategy:
    """Configuration for ModelRouter."""

    name: str = "primary"
    cost_threshold_chars: int | None = None
    ab_split: float = 0.5


class ModelRouter(ModelBackend):
    """Route ChatRequest across multiple backends with configurable strategy.

    Supported strategies:
      - ``primary``: always use the first backend; fallback through the list.
      - ``cost``: route to the second backend when input exceeds a character
        threshold, otherwise use the first.
      - ``quality``: route to the second backend when tools are present
        (heuristic: more capable model for tool use).
      - ``ab``: random 50/50 split between the first two backends.

    """

    name = "router"
    supports_tool_calls = True
    supports_streaming = True

    def __init__(
        self,
        backends: list[ModelBackend],
        strategy: RoutingStrategy | None = None,
        *,
        event_bus: EventBus | None = None,
        session_id: str = "",
        agent_name: str = "",
    ) -> None:
        if not backends:
            raise ValueError("ModelRouter requires at least one backend")
        self._backends = list(backends)
        self._strategy = strategy or RoutingStrategy()
        self._event_bus = event_bus
        self._session_id = session_id
        self._agent_name = agent_name

    async def chat(self, request: ChatRequest) -> ChatResponse:
        ordered = self._ordered_backends(request)
        last_error: Exception | None = None
        for idx, backend in enumerate(ordered):
            try:
                response = await backend.chat(request)
                if response.backend != backend.name:
                    response = _with_backend(response, backend.name)
                await self._emit_fallback_event(request, backend, None)
                return response
            except (ModelUnavailable, asyncio.TimeoutError) as exc:
                last_error = exc
                reason = getattr(exc, "reason", str(exc))
                next_backend = ordered[idx + 1].name if idx + 1 < len(ordered) else "none"
                logger.warning("Backend %s failed: %s", backend.name, reason)
                record_model_router_fallback(backend.name, next_backend, reason)
                await self._emit_fallback_event(request, backend, reason)
            except Exception as exc:
                last_error = exc
                next_backend = ordered[idx + 1].name if idx + 1 < len(ordered) else "none"
                logger.exception("Backend %s raised unexpected error", backend.name)
                record_model_router_fallback(backend.name, next_backend, str(exc))
                await self._emit_fallback_event(request, backend, str(exc))
        raise NoBackendAvailable(request) from last_error

    async def stream(
        self, request: ChatRequest
    ) -> AsyncIterator[TokenChunk | ChatResponse]:
        """Stream from the selected backend, falling back on failures.

        The first backend that supports streaming and does not raise is used;
        its iterator is yielded transparently. If all streaming-capable
        backends fail, ``NoBackendAvailable`` is raised.
        """
        ordered = self._ordered_backends(request)
        streaming_backends = [b for b in ordered if b.supports_streaming]
        last_error: Exception | None = None
        for idx, backend in enumerate(streaming_backends):
            try:
                await self._emit_fallback_event(request, backend, None)
                async for chunk in backend.stream(request):
                    yield chunk
                return
            except (ModelUnavailable, asyncio.TimeoutError) as exc:
                last_error = exc
                reason = getattr(exc, "reason", str(exc))
                next_backend = (
                    streaming_backends[idx + 1].name
                    if idx + 1 < len(streaming_backends)
                    else "none"
                )
                logger.warning("Backend %s stream failed: %s", backend.name, reason)
                record_model_router_fallback(backend.name, next_backend, reason)
                await self._emit_fallback_event(request, backend, reason)
            except Exception as exc:
                last_error = exc
                next_backend = (
                    streaming_backends[idx + 1].name
                    if idx + 1 < len(streaming_backends)
                    else "none"
                )
                logger.exception("Backend %s stream raised unexpected error", backend.name)
                record_model_router_fallback(backend.name, next_backend, str(exc))
                await self._emit_fallback_event(request, backend, str(exc))
        if last_error is None:
            raise NoBackendAvailable(request)
        raise NoBackendAvailable(request) from last_error

    def _ordered_backends(self, request: ChatRequest) -> list[ModelBackend]:
        strategy = self._strategy.name
        if strategy == "primary" or len(self._backends) == 1:
            return list(self._backends)
        if strategy == "cost":
            chars = sum(len(m.content or "") for m in request.messages)
            threshold = self._strategy.cost_threshold_chars
            if threshold is not None and chars > threshold:
                return [self._backends[1], self._backends[0]] + list(self._backends[2:])
            return list(self._backends)
        if strategy == "quality":
            if request.tools:
                return [self._backends[1], self._backends[0]] + list(self._backends[2:])
            return list(self._backends)
        if strategy == "ab" and len(self._backends) >= 2:
            return (
                [self._backends[0], self._backends[1]]
                if random.random() < self._strategy.ab_split
                else [self._backends[1], self._backends[0]]
            )
        return list(self._backends)

    async def _emit_fallback_event(
        self,
        request: ChatRequest,
        backend: ModelBackend,
        reason: str | None,
    ) -> None:
        if self._event_bus is None:
            return
        event_type = "model.fallback" if reason else "model.selected"
        payload: dict[str, Any] = {
            "model": request.model,
            "backend": backend.name,
            "strategy": self._strategy.name,
        }
        if reason:
            payload["reason"] = reason
        await self._event_bus.publish(
            AgentEvent(
                type=event_type,  # type: ignore[arg-type]
                session_id=self._session_id,
                agent_name=self._agent_name,
                turn_index=0,
                payload=payload,
            )
        )


def _with_backend(response: ChatResponse, backend: str) -> ChatResponse:
    if response.backend == backend:
        return response
    return ChatResponse(
        backend=backend,
        model=response.model,
        message=response.message,
        usage=response.usage,
        finish_reason=response.finish_reason,
        latency_ms=response.latency_ms,
        raw=response.raw,
    )
