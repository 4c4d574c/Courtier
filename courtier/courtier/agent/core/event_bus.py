"""In-process async event bus for AgentEvents.

The event bus decouples event producers (``agent_loop``) from consumers
(SSE adapter, audit logger, telemetry, etc.). It supports filtering by
``event_type`` and ``session_id`` and provides configurable backpressure.
"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator

from ..telemetry.metrics import record_event_bus_dropped
from .events import AgentEvent, EventType

logger = logging.getLogger(__name__)


BackpressureStrategy = str  # "drop_oldest" | "drop_newest" | "block"


class EventSubscription:
    """A filtered subscription to an ``EventBus``.

    Iterate asynchronously over matching events:

        async for event in subscription:
            ...

    The iterator skips events that do not match the configured filters.
    """

    def __init__(
        self,
        queue: asyncio.Queue[AgentEvent],
        *,
        event_types: set[EventType] | None = None,
        session_id: str | None = None,
        kind: str = "default",
        overflow: BackpressureStrategy | None = None,
    ) -> None:
        self.queue = queue
        self.event_types = event_types
        self.session_id = session_id
        #: Subscription class label (metric/canary dimension).
        self.kind = kind
        #: Per-subscription overflow override; falls back to the bus default.
        self.overflow = overflow

    def matches(self, event: AgentEvent) -> bool:
        if self.session_id is not None and event.session_id != self.session_id:
            return False
        if self.event_types is not None and event.type not in self.event_types:
            return False
        return True

    def __aiter__(self) -> AsyncIterator[AgentEvent]:
        return self

    async def __anext__(self) -> AgentEvent:
        while True:
            event = await self.queue.get()
            if self.matches(event):
                return event


class EventBus:
    """Publish/subscribe bus for ``AgentEvent`` instances.

    Note on backpressure: with the ``block`` strategy a single stalled
    subscriber blocks every publisher — including the agent loop itself.
    Prefer the default ``drop_oldest`` for streaming consumers such as SSE.

    Example::

        bus = EventBus()
        sub = bus.subscribe(session_id="sess_123")
        await bus.publish(AgentEvent(...))
        event = await sub.queue.get()
    """

    def __init__(
        self,
        *,
        default_maxsize: int = 1000,
        backpressure: BackpressureStrategy = "drop_oldest",
    ) -> None:
        self._subscriptions: list[EventSubscription] = []
        self.default_maxsize = default_maxsize
        self.backpressure = backpressure

    def subscribe(
        self,
        *,
        event_types: set[EventType] | None = None,
        session_id: str | None = None,
        maxsize: int | None = None,
        kind: str = "default",
        overflow: BackpressureStrategy | None = None,
    ) -> EventSubscription:
        """Create a new filtered subscription.

        Args:
            event_types: If provided, only events of these types are delivered.
            session_id: If provided, only events for this session are delivered.
            maxsize: Per-subscriber queue size. Defaults to ``default_maxsize``.
            kind: Subscriber class label (metrics/canary dimension).
            overflow: Per-subscription backpressure override.
        """
        queue: asyncio.Queue[AgentEvent] = asyncio.Queue(
            maxsize=maxsize if maxsize is not None else self.default_maxsize
        )
        subscription = EventSubscription(
            queue,
            event_types=event_types,
            session_id=session_id,
            kind=kind,
            overflow=overflow,
        )
        self._subscriptions.append(subscription)
        return subscription

    async def publish(self, event: AgentEvent) -> None:
        """Publish an event to all matching subscriptions."""
        for subscription in list(self._subscriptions):
            if not subscription.matches(event):
                continue
            queue = subscription.queue
            strategy = subscription.overflow or self.backpressure
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                if strategy == "drop_oldest":
                    try:
                        dropped = queue.get_nowait()
                        logger.debug(
                            "Event bus dropped oldest event %s for subscriber",
                            dropped.event_id,
                        )
                    except asyncio.QueueEmpty:
                        pass
                    queue.put_nowait(event)
                    record_event_bus_dropped(event.type, self.backpressure)
                elif strategy == "drop_newest":
                    logger.debug(
                        "Event bus dropped newest event %s for subscriber (queue full)",
                        event.event_id,
                    )
                    record_event_bus_dropped(event.type, strategy)
                elif strategy == "block":
                    await queue.put(event)
                else:
                    # Unknown strategy: drop newest as safe fallback.
                    logger.warning(
                        "Unknown backpressure strategy %r; dropping event %s",
                        self.backpressure,
                        event.event_id,
                    )
                    record_event_bus_dropped(event.type, strategy)

    def unsubscribe(self, subscription: EventSubscription) -> None:
        """Remove a subscription from the bus."""
        try:
            self._subscriptions.remove(subscription)
        except ValueError:
            pass
