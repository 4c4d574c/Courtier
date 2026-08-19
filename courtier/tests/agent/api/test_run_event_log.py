"""Tests for RunEventLog — bounded, boundary-evicting, replayable SSE transcript."""

import pytest

from courtier.agent.api.services.run_event_log import (
    RESYNC_SEQ,
    RunEventLog,
    render_sse_line,
)


def _append(log: RunEventLog, text: str, **kwargs) -> int:
    return log.append({"type": "token", "text": text}, **kwargs)


class TestSeqAssignment:
    def test_append_assigns_monotonic_seqs(self):
        log = RunEventLog()
        assert log.append({"type": "a"}) == 0
        assert log.append({"type": "b"}) == 1
        assert log.last_seq == 1
        assert log.next_seq == 2

    def test_initial_seq_for_continuation_runs(self):
        log = RunEventLog(initial_seq=101)
        assert log.append({"type": "a"}) == 101
        # Previous run's watermark (100) still resolves cleanly.
        assert log.replay_after(100) is not None
        assert log.replay_after(99) is None

    def test_reserve_then_append_keeps_seq_order(self):
        log = RunEventLog()
        reserved = log.reserve()
        assert reserved == 0
        log.append({"type": "other"})  # auto seq 1, appended first
        log.append({"type": "reserved"}, seq=reserved)  # late reserved seq
        entries = log.replay_after(-1)
        assert [e.seq for e in entries] == [0, 1]
        assert entries[0].payload["type"] == "reserved"

    def test_reserve_without_append_leaves_skippable_hole(self):
        log = RunEventLog()
        log.reserve()  # reserved, never appended (simulated failure)
        log.append({"type": "a"})
        entries = log.replay_after(-1)
        assert [e.seq for e in entries] == [1]

    def test_line_format_has_id_and_data(self):
        line = render_sse_line(7, {"type": "token", "text": "你好"})
        assert line.startswith("id: 7\ndata: ")
        assert line.endswith("\n\n")
        assert "你好" in line
        log = RunEventLog()
        seq = log.append({"type": "token", "text": "x"})
        assert log.replay_after(-1)[0].line == render_sse_line(seq, {"type": "token", "text": "x"})

    def test_append_after_seal_is_noop(self):
        log = RunEventLog()
        log.append({"type": "a"})
        log.seal()
        assert log.append({"type": "b"}) == RESYNC_SEQ
        assert len(log) == 1
        assert log.sealed


class TestReplay:
    def test_replay_after_windows(self):
        log = RunEventLog()
        for i in range(5):
            log.append({"type": "token", "text": str(i)})
        assert [e.seq for e in log.replay_after(-1)] == [0, 1, 2, 3, 4]
        assert [e.seq for e in log.replay_after(2)] == [3, 4]
        assert log.replay_after(4) == []
        # Watermark at the exact end of a fresh log resolves to empty replay.
        assert log.replay_after(log.last_seq) == []

    def test_empty_log_replay_at_initial_boundary(self):
        log = RunEventLog(initial_seq=50)
        assert log.replay_after(49) == []
        assert log.replay_after(48) is None


class TestBoundaryEviction:
    def test_eviction_only_before_boundaries(self):
        log = RunEventLog(max_events=3)
        _append(log, "t0")  # seq 0 — pre-boundary, evictable
        _append(log, "t1")  # seq 1
        b = _append(log, "b")  # seq 2 — boundary (think/step start)
        log.mark_boundary(b)
        _append(log, "t2")  # seq 3
        _append(log, "t3")  # seq 4 → over cap, evicts everything before boundary
        assert log.first_seq == b
        # Watermarks before the retained range must resync...
        assert log.replay_after(-1) is None
        assert log.replay_after(0) is None
        # ...while watermark at the last evicted entry resolves to the tail
        # (the boundary event itself is retained and deliverable).
        assert [e.seq for e in log.replay_after(1)] == [2, 3, 4]
        assert log.truncated is False
        assert log.evicted_count == 2

    def test_byte_cap_evicts_to_boundaries(self):
        # Lines are ~51 bytes; 180 keeps the overflow point past the first
        # marked boundary (eviction triggers on the 4th append).
        log = RunEventLog(max_events=10_000, max_bytes=180)
        for i in range(20):
            seq = _append(log, f"token-{i}")
            if i % 2 == 0:
                log.mark_boundary(seq)
        assert len(log) < 20
        assert log.first_seq >= 0
        assert log.first_seq % 2 == 0  # landed on a boundary
        assert log.truncated is False

    def test_truncated_fallback_without_boundaries(self):
        log = RunEventLog(max_events=2)
        for i in range(6):
            _append(log, f"t{i}")  # eviction runs after every append
        assert log.truncated is True
        assert len(log) == 2
        assert [e.seq for e in log.replay_after(3)] == [4, 5]
        assert log.replay_after(2) is None

    def test_stale_boundary_not_recorded(self):
        log = RunEventLog(max_events=3)
        _append(log, "a")  # seq 0
        b = _append(log, "b")  # seq 1, boundary
        log.mark_boundary(b)
        _append(log, "c")
        _append(log, "d")  # evicts seq 0; first_seq == 1
        assert log.first_seq == b
        log.mark_boundary(0)  # predates first_seq → ignored
        assert log._boundaries == []


class TestReaders:
    async def test_reader_replay_then_live_then_seal(self):
        log = RunEventLog()
        _append(log, "a")
        _append(log, "b")
        reader = log.reader(since=-1)
        assert (await reader.__anext__()).seq == 0  # replay: a
        assert (await reader.__anext__()).seq == 1  # replay: b
        _append(log, "c")
        assert (await reader.__anext__()).seq == 2  # live: c
        log.seal()
        with pytest.raises(StopAsyncIteration):
            await reader.__anext__()
        # Sealed readers stay exhausted.
        with pytest.raises(StopAsyncIteration):
            await reader.__anext__()

    async def test_reader_live_only_when_since_none(self):
        log = RunEventLog()
        _append(log, "missed")
        reader = log.reader()  # eager registration
        _append(log, "seen")  # appended AFTER registration → queued
        item = await reader.__anext__()
        assert item.payload["text"] == "seen"
        log.seal()
        reader.aclose()

    async def test_reader_resync_when_watermark_evicted(self):
        log = RunEventLog(max_events=2)
        _append(log, "a")
        b = _append(log, "b")
        log.mark_boundary(b)
        _append(log, "c")
        _append(log, "d")  # evicts "a"
        reader = log.reader(since=0)
        item = await reader.__anext__()
        assert item.seq == RESYNC_SEQ
        with pytest.raises(StopAsyncIteration):
            await reader.__anext__()

    async def test_reader_resync_when_overflowed(self):
        log = RunEventLog(reader_queue_size=2)
        reader = log.reader()  # registered, not drained
        for i in range(5):
            _append(log, f"t{i}")
        item = await reader.__anext__()
        assert item.seq == RESYNC_SEQ

    async def test_reader_delivers_late_reserved_seq(self):
        log = RunEventLog()
        _append(log, "a")
        reader = log.reader()  # live-only; last_seq == 0
        reserved = log.reserve()  # seq 1
        log.append({"type": "token", "text": "r"}, seq=reserved)
        item = await reader.__anext__()
        assert item.seq == reserved

    async def test_seal_wakes_idle_reader(self):
        log = RunEventLog()
        reader = log.reader()
        log.seal()
        with pytest.raises(StopAsyncIteration):
            await reader.__anext__()

    async def test_multiple_readers_independent(self):
        log = RunEventLog()
        _append(log, "a")
        r1 = log.reader(since=-1)
        assert (await r1.__anext__()).seq == 0
        r2 = log.reader(since=-1)
        assert (await r2.__anext__()).seq == 0
        _append(log, "z")
        assert (await r1.__anext__()).seq == 1
        assert (await r2.__anext__()).seq == 1
        assert log.reader_count == 2
        r1.aclose()
        assert log.reader_count == 1
        log.seal()

    async def test_async_for_iteration(self):
        log = RunEventLog()
        _append(log, "a")
        _append(log, "b")
        reader = log.reader(since=-1)
        seen = []
        log.seal()
        # seal() arrived after both entries: replay drains, then iteration ends.
        async for entry in reader:
            seen.append(entry.seq)
        assert seen == [0, 1]
