"""RunEventLog — bounded, replayable SSE transcript for one agent run.

Single-writer (``RunRecorder``), many-readers (SSE connections). The log
stores the *final* SSE payloads as pre-rendered ``id:/data:`` lines, so
replay never re-runs adapter logic — persistence side effects can never be
duplicated by a re-attaching observer.

Invariants (see docs/superpowers/plans/2026-08-19-background-task-queue-implementation.md):

- I1 single writer: only ``append``/``reserve`` callers produce entries.
- I2 watermark: ``snapshot(event_seq=W) + entries with seq > W`` reconstructs
  the full frontend state. Callers persist to the SessionStore *with* the
  reserved seq *before* appending the event at that seq (reserve → persist →
  append), so the snapshot watermark and the log can never disagree.
- I3 boundary-aligned eviction: entries are only evicted *before* a marked
  safe boundary (step/turn start). Everything before a boundary is already
  persisted in the SessionStore, so ``snapshot + retained tail`` always
  reconstructs cleanly. When the requested watermark lies before the oldest
  retained entry, readers surface a ``resync`` marker instead of a gap.

Seq numbers are per-session monotonic across runs: a continuation run
creates its log with ``initial_seq = record.event_seq + 1`` so attach
replays stay aligned with the persisted watermark.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

#: Sentinel seq for the resync marker (never a legal event seq).
RESYNC_SEQ = -1


def render_sse_line(seq: int, payload: dict[str, Any]) -> str:
    """Render one SSE frame with an explicit id so EventSource reconnects
    can resume via ``Last-Event-ID``.  ``default=str`` keeps a stray
    non-JSON-serializable value (e.g. plugin metadata) from raising and
    killing the event stream."""
    data = json.dumps(payload, ensure_ascii=False, default=str)
    return f"id: {seq}\ndata: {data}\n\n"


@dataclass(frozen=True)
class LogEntry:
    seq: int
    payload: dict[str, Any]
    line: str
    size: int


def resync_entry() -> LogEntry:
    """Marker telling the client its watermark predates the retained log."""
    payload = {"type": "resync"}
    return LogEntry(RESYNC_SEQ, payload, f"data: {json.dumps(payload)}\n\n", 0)


class _ReaderState:
    """Per-connection live tail: a bounded queue fed by ``append``."""

    def __init__(self, queue_size: int) -> None:
        self.queue: asyncio.Queue[LogEntry | None] = asyncio.Queue(maxsize=queue_size)
        # Set when an append found the queue full — the reader missed events
        # and must resync instead of streaming a silent gap.
        self.overflowed = False
        self.sealed = False

    def notify(self, entry: LogEntry) -> None:
        if self.overflowed:
            return
        try:
            self.queue.put_nowait(entry)
        except asyncio.QueueFull:
            self.overflowed = True
            logger.warning(
                "Run event log reader queue full; reader flagged for resync at seq %s",
                entry.seq,
            )

    def seal(self) -> None:
        self.sealed = True
        try:
            self.queue.put_nowait(None)
        except asyncio.QueueFull:
            # The reader is behind; it will observe ``sealed`` once it drains.
            pass


class RunEventReader:
    """Eagerly-registered reader: replay first, then live tail until seal."""

    def __init__(
        self,
        log: "RunEventLog",
        state: _ReaderState,
        replay: list[LogEntry],
        last_delivered: int,
    ) -> None:
        self._log = log
        self._state = state
        self._pending = replay
        self._last = last_delivered
        self._done = False

    def __aiter__(self) -> "RunEventReader":
        return self

    async def __anext__(self) -> LogEntry:
        if self._done:
            raise StopAsyncIteration
        if self._pending:
            item = self._pending.pop(0)
            if item.seq == RESYNC_SEQ:
                self._done = True
                return item
            self._last = item.seq
            return item
        state = self._state
        while True:
            if state.overflowed:
                self._done = True
                return resync_entry()
            item = await state.queue.get()
            if item is None:
                if state.sealed:
                    self._done = True
                    raise StopAsyncIteration
                continue
            if item.seq <= self._last:
                # Already delivered via the replay snapshot taken at
                # registration — the queue may hold that overlap.
                continue
            self._last = item.seq
            return item

    def aclose(self) -> None:
        self._done = True
        self._log._detach_reader(self._state)


class RunEventLog:
    """Bounded event transcript for one run.

    Args:
        max_events: Entry count cap — eviction kicks in beyond it.
        max_bytes: Rendered byte cap — eviction kicks in beyond it.
        initial_seq: First seq to assign (default 0). Continuation runs pass
            ``record.event_seq + 1`` so seqs stay monotonic per session.
        reader_queue_size: Per-reader live queue bound (test hook).
    """

    def __init__(
        self,
        *,
        max_events: int = 50_000,
        max_bytes: int = 8 * 1024 * 1024,
        initial_seq: int = 0,
        reader_queue_size: int = 1000,
    ) -> None:
        self._entries: list[LogEntry] = []
        self._boundaries: list[int] = []
        self._next_seq = initial_seq
        # Seq of the oldest entry that *would* be retained — equals
        # ``initial_seq`` until the first eviction.
        self._first_seq = initial_seq
        self._total_bytes = 0
        self._max_events = max_events
        self._max_bytes = max_bytes
        self._reader_queue_size = reader_queue_size
        self._readers: list[_ReaderState] = []
        self._sealed = False
        self._truncated = False
        self._evicted = 0

    # -- Writing (RunRecorder only) ---------------------------------------------

    def reserve(self) -> int:
        """Pre-allocate the next seq (reserve → persist → append order)."""
        seq = self._next_seq
        self._next_seq += 1
        return seq

    def append(self, payload: dict[str, Any], *, seq: int | None = None) -> int:
        """Append one rendered event and fan out to live readers.

        Args:
            payload: Final SSE payload dict.
            seq: A seq previously returned by ``reserve`` (allows persisting
                to the SessionStore between reservation and append). When
                omitted the next seq is assigned implicitly.

        Returns:
            The seq actually used (``RESYNC_SEQ`` and a no-op if sealed).
        """
        if self._sealed:
            logger.warning("Run event log sealed; dropped event %r", payload.get("type"))
            return RESYNC_SEQ
        if seq is None:
            seq = self.reserve()
        line = render_sse_line(seq, payload)
        entry = LogEntry(seq, payload, line, len(line.encode("utf-8")))
        self._insert_ordered(entry)
        self._total_bytes += entry.size
        for reader in self._readers:
            reader.notify(entry)
        self._maybe_evict()
        return seq

    def mark_boundary(self, seq: int) -> None:
        """Mark ``seq`` (an appended entry) as a safe eviction boundary.

        Everything strictly before a boundary is considered persisted in the
        SessionStore, so it may be dropped once caps are exceeded.
        """
        if seq < self._first_seq:
            return
        if not self._boundaries or seq > self._boundaries[-1]:
            self._boundaries.append(seq)

    def seal(self) -> None:
        """Mark the run terminal: further appends are dropped, readers end."""
        if self._sealed:
            return
        self._sealed = True
        for reader in self._readers:
            reader.seal()

    # -- Reading -----------------------------------------------------------------

    def replay_after(self, since: int) -> list[LogEntry] | None:
        """All retained entries with ``seq > since``.

        Returns None when ``since`` predates the oldest retained entry — the
        caller must resync (reload snapshot, re-attach) instead of streaming
        a gap. Skips holes left by reserve-then-fail sequences.
        """
        if since + 1 < self._first_seq:
            return None
        return [e for e in self._entries if e.seq > since]

    def reader(self, since: int | None = None) -> RunEventReader:
        """Attach a reader streaming entries with ``seq > since``, then live.

        Registration is eager (synchronous): everything appended after this
        call is queued for the reader even before its first ``__anext__``.

        ``since=None`` attaches live-only (no replay) — used by the
        initiating connection, which has nothing to catch up.

        When the requested window was evicted, the reader's first item is a
        resync marker (seq == RESYNC_SEQ) and the stream ends there.
        """
        state = _ReaderState(self._reader_queue_size)
        if self._sealed:
            # Registered after seal(): no live events will ever arrive, so the
            # reader must terminate once its replay drains (otherwise the None
            # sentinel never arrives and the live loop waits forever).
            state.seal()
        self._readers.append(state)
        if since is None:
            # Live-only readers skip history; use last_seq (not next_seq) so
            # an in-flight reserved seq still delivers.
            return RunEventReader(self, state, [], self.last_seq)
        replay = self.replay_after(since)
        if replay is None:
            return RunEventReader(self, state, [resync_entry()], since)
        last = replay[-1].seq if replay else since
        return RunEventReader(self, state, replay, last)

    # -- Introspection -------------------------------------------------------------

    @property
    def last_seq(self) -> int:
        """Seq of the most recently appended entry (``initial_seq - 1`` when empty)."""
        return self._entries[-1].seq if self._entries else self._next_seq - 1

    @property
    def next_seq(self) -> int:
        return self._next_seq

    @property
    def first_seq(self) -> int:
        return self._first_seq

    @property
    def sealed(self) -> bool:
        return self._sealed

    @property
    def truncated(self) -> bool:
        """True when caps forced dropping non-boundary-aligned history."""
        return self._truncated

    @property
    def evicted_count(self) -> int:
        return self._evicted

    @property
    def reader_count(self) -> int:
        return len(self._readers)

    def __len__(self) -> int:
        return len(self._entries)

    # -- Internal -------------------------------------------------------------------

    def _insert_ordered(self, entry: LogEntry) -> None:
        """Insert keeping seq order. Appends are normally in-order; a rare
        reserved seq arriving after a higher auto seq (interleaved async
        writer) is repaired here so replay stays monotonic."""
        if not self._entries or entry.seq > self._entries[-1].seq:
            self._entries.append(entry)
            return
        for i in range(len(self._entries) - 1, -1, -1):
            if self._entries[i].seq < entry.seq:
                self._entries.insert(i + 1, entry)
                return
        self._entries.insert(0, entry)

    def _detach_reader(self, state: _ReaderState) -> None:
        try:
            self._readers.remove(state)
        except ValueError:
            pass

    def _maybe_evict(self) -> None:
        """Drop history before safe boundaries until under both caps.

        Boundary-by-boundary from the oldest; if no boundary is available
        (single oversized step) fall back to dropping oldest entries and set
        ``truncated`` — replay for watermarks in the dropped range then
        resyncs, which is correct but loses that step's animation continuity.
        """
        while len(self._entries) > self._max_events or self._total_bytes > self._max_bytes:
            while self._boundaries and self._boundaries[0] <= self._first_seq:
                self._boundaries.pop(0)
            if not self._boundaries or not self._entries:
                break
            boundary = self._boundaries.pop(0)
            while self._entries and self._entries[0].seq < boundary:
                dropped = self._entries.pop(0)
                self._total_bytes -= dropped.size
                self._evicted += 1
            self._first_seq = self._entries[0].seq if self._entries else boundary
        if len(self._entries) > self._max_events or self._total_bytes > self._max_bytes:
            self._truncated = True
            while len(self._entries) > self._max_events or self._total_bytes > self._max_bytes:
                if not self._entries:
                    break
                dropped = self._entries.pop(0)
                self._total_bytes -= dropped.size
                self._evicted += 1
            if self._entries:
                self._first_seq = self._entries[0].seq
