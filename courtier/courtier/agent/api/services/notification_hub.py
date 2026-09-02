"""NotificationHub — per-user run-status fan-out for the global events channel.

Distinct from the run EventBus: these are *user-level*, very low-frequency
status transitions (queued / started / completed / error / stopped), pushed
to every open ``GET /api/events`` connection of the owning user. No replay —
on reconnect the client re-fetches the session list to close any gap.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

logger = logging.getLogger(__name__)


@dataclass
class _Connection:
    user: str
    queue: asyncio.Queue[str] = field(default_factory=lambda: asyncio.Queue(maxsize=256))


class NotificationHub:
    """Tracks open global-events connections and broadcasts status changes."""

    def __init__(self) -> None:
        self._connections: list[_Connection] = []

    def subscribe(self, user: str) -> _Connection:
        conn = _Connection(user=user)
        self._connections.append(conn)
        return conn

    def unsubscribe(self, conn: _Connection) -> None:
        try:
            self._connections.remove(conn)
        except ValueError:
            pass

    def publish(
        self,
        user: str,
        *,
        session_id: str,
        status: str,
        queue_position: int | None = None,
        conclusion: str = "",
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        pending_confirmations: int | None = None,
    ) -> None:
        """Fan a run-status transition out to the owner's connections.

        Fire-and-forget: a slow or stale connection drops its oldest pending
        event (the next reconnect + list refetch realigns it anyway).
        """
        payload: dict[str, Any] = {
            "type": "run_status",
            "sessionId": session_id,
            "status": status,
        }
        if queue_position is not None:
            payload["queuePosition"] = queue_position
        if conclusion:
            payload["conclusion"] = conclusion[:200]
        if tokens_in is not None:
            payload["tokensIn"] = tokens_in
        if tokens_out is not None:
            payload["tokensOut"] = tokens_out
        if pending_confirmations is not None:
            payload["pendingConfirmations"] = pending_confirmations
        line = f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        for conn in list(self._connections):
            if conn.user != user:
                continue
            try:
                conn.queue.put_nowait(line)
            except asyncio.QueueFull:
                try:
                    conn.queue.get_nowait()  # drop oldest
                    conn.queue.put_nowait(line)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    async def stream(
        self, conn: _Connection, *, heartbeat_seconds: float = 25.0
    ) -> AsyncIterator[str]:
        """Yield queued events with keep-alive comments until unsubscribed."""
        try:
            while True:
                try:
                    line = await asyncio.wait_for(conn.queue.get(), timeout=heartbeat_seconds)
                    yield line
                except asyncio.TimeoutError:
                    # Comment heartbeat: keeps proxies/browsers from idling out.
                    yield ": hb\n\n"
        finally:
            self.unsubscribe(conn)

    @property
    def connection_count(self) -> int:
        return len(self._connections)
