"""Tests for RunManager — run lifecycle decoupled from SSE connections."""

from __future__ import annotations

import asyncio
import json
import tempfile
from types import SimpleNamespace

import pytest

from courtier.agent.agents.base import Agent
from courtier.agent.api.services.run_manager import (
    RunConflictError,
    RunManager,
    RunSpec,
    stream_run,
)
from courtier.agent.api.session_store import SessionStore
from courtier.agent.core.model import MockModelClient, ToolCall
from courtier.agent.tools.builtin.echo import EchoTool


class _Settings:
    """Minimal settings stand-in for RunManager."""

    audit_log_enabled = False
    audit_log_dir = ""
    run_grace_seconds = 600
    run_log_max_events = 50_000
    run_log_max_bytes = 8 * 1024 * 1024
    llm_api_key = ""
    llm_base_url = ""
    llm_model = "mock"


class _SlowAgent:
    """Agent stub whose run blocks until cancelled."""

    tool_registry = None

    def __init__(self) -> None:
        self.model = SimpleNamespace(close=_noop_close)

    async def run(self, **kwargs):
        await asyncio.sleep(60)


async def _noop_close() -> None:
    return


def _echo_agent() -> Agent:
    tc = ToolCall(id="1", name="echo", arguments={"text": "hi"})
    return Agent(
        name="EchoAgent",
        role="Echo back text using the echo tool.",
        tools=[EchoTool()],
        model=MockModelClient(tool_calls=[tc]),
    )


def _spec(store: SessionStore, session_id: str, agent=None, **kw) -> RunSpec:
    return RunSpec(
        session_id=session_id,
        user="alice",
        task="hello",
        agent=agent or _echo_agent(),
        **kw,
    )


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as d:
        yield SessionStore(d)


@pytest.fixture
def manager(store):
    return RunManager(session_store=store, settings=_Settings())


async def _create(store, sid="sess_7e57e57e57e5") -> None:
    await store.create(sid, "task", "")


class TestRunLifecycle:
    async def test_run_completes_without_observers(self, manager, store):
        """核心不变式:无任何 SSE 消费者时 run 照常跑完并完整落盘。"""
        await _create(store)
        run = await manager.start(_spec(store, "sess_7e57e57e57e5", is_new=True))
        await run.task

        assert run.status == "completed"
        session = await store.get("sess_7e57e57e57e5")
        assert session is not None
        assert session.status == "completed"
        assert session.conclusion == "Done."
        assert session.messages_json  # final state persisted
        assert run.log.sealed
        terminal = run.log.replay_after(-1)[-1]
        assert terminal.payload["type"] == "complete"

    async def test_run_survives_disconnect(self, manager, store):
        """中途断开连接只 detach 观察者,run 继续。"""
        await _create(store)
        run = await manager.start(_spec(store, "sess_7e57e57e57e5", is_new=True))
        gen = stream_run(run)
        # 消费第一条事件后“离开页面”
        await gen.__anext__()
        await gen.aclose()
        assert not run.terminal

        await run.task
        assert run.status == "completed"
        session = await store.get("sess_7e57e57e57e5")
        assert session is not None
        assert session.status == "completed"

    async def test_duplicate_start_conflicts(self, manager, store):
        await _create(store)
        await manager.start(_spec(store, "sess_7e57e57e57e5"))
        with pytest.raises(RunConflictError):
            await manager.start(_spec(store, "sess_7e57e57e57e5"))

    async def test_terminal_status_promotion_and_session_event(self, manager, store):
        await _create(store)
        run = await manager.start(_spec(store, "sess_7e57e57e57e5", is_new=True))
        await run.task  # runner finished → full transcript (including terminal)
        entries = run.log.replay_after(-1)
        assert entries[0].payload["type"] == "session"
        assert entries[0].payload["sessionId"] == "sess_7e57e57e57e5"
        session = await store.get("sess_7e57e57e57e5")
        assert session is not None
        assert session.status == "completed"

    async def test_terminal_event_stamps_watermark(self, manager, store):
        """终态事件的 seq 打在 store 水位上:快照+重放不会重复 complete。"""
        await _create(store)
        run = await manager.start(_spec(store, "sess_7e57e57e57e5", is_new=True))
        await run.task
        session = await store.get("sess_7e57e57e57e5")
        terminal = run.log.replay_after(-1)[-1]
        assert session.event_seq == terminal.seq
        assert terminal.payload["type"] == "complete"


class TestStop:
    async def test_stop_cancels_and_persists_stopped(self, manager, store):
        await _create(store)
        run = await manager.start(_spec(store, "sess_7e57e57e57e5", agent=_SlowAgent()))
        await asyncio.sleep(0)  # let the runner promote the session to running
        assert manager.has_active("sess_7e57e57e57e5")
        stopped = await manager.stop("sess_7e57e57e57e5")
        assert stopped is True
        await asyncio.sleep(0)  # cancellation handlers run over several yields

        session = await store.get("sess_7e57e57e57e5")
        assert session is not None
        assert session.status == "stopped"
        assert session.finished_at is not None
        # Cancelled before the recorder was created → log sealed without a
        # terminal event (cancelled runs emit no transcript tail here).
        assert run.log.sealed

    async def test_stop_absent_session_returns_false(self, manager, store):
        assert await manager.stop("sess_7e57e57e57e5") is False

    async def test_stop_all(self, manager, store):
        await _create(store, "sess_7e57e57e57e5")
        await _create(store, "sess_7e57e57e0002")
        await manager.start(_spec(store, "sess_7e57e57e57e5", agent=_SlowAgent()))
        await manager.start(_spec(store, "sess_7e57e57e0002", agent=_SlowAgent()))
        stopped = await manager.stop_all()
        assert sorted(stopped) == ["sess_7e57e57e0002", "sess_7e57e57e57e5"]


class TestAttachAndGrace:
    async def test_attach_replays_missed_events(self, manager, store):
        """run 进行中 attach:since 水位之后的事件全部补齐。"""
        await _create(store)
        run = await manager.start(_spec(store, "sess_7e57e57e57e5", is_new=True))
        await run.task  # let it finish (log now sealed, run in grace)

        attached = manager.attach("sess_7e57e57e57e5", since=-1)
        assert attached is not None
        _, reader = attached
        # The log is sealed → synchronous enumeration via replay.
        entries = run.log.replay_after(-1)
        types = [e.payload["type"] for e in entries]
        assert types[0] == "session"
        assert types[-1] == "complete"
        assert "think" in types and "tool_result" in types

    async def test_attach_within_grace_after_terminal(self, manager, store):
        await _create(store)
        run = await manager.start(_spec(store, "sess_7e57e57e57e5"))
        await run.task
        assert run.terminal
        assert manager.attach("sess_7e57e57e57e5") is not None
        assert manager.has_active("sess_7e57e57e57e5")

    async def test_grace_expiry_detaches_history(self, manager, store):
        await _create(store)
        run = await manager.start(_spec(store, "sess_7e57e57e57e5"))
        await run.task
        run.finished_at -= 601  # beyond run_grace_seconds
        assert manager.attach("sess_7e57e57e57e5") is None
        assert not manager.has_active("sess_7e57e57e57e5")

    async def test_attach_absent_run(self, manager):
        assert manager.attach("sess_7e57e57e57e5") is None


class TestStreamRun:
    async def test_lines_carry_id_prefix(self, manager, store):
        await _create(store)
        run = await manager.start(_spec(store, "sess_7e57e57e57e5", is_new=True))
        lines = []
        async for line in stream_run(run):
            lines.append(line)
        assert lines
        for line in lines:
            assert line.startswith("id: ")
            assert "\ndata: " in line
        payload_types = [json.loads(line.split("data: ", 1)[1])["type"] for line in lines]
        assert payload_types[0] == "session"
        assert payload_types[-1] == "complete"

    async def test_continuation_seq_continues_watermark(self, manager, store):
        """续轮 run 的日志 seq 接续上一轮水位(全 session 单调)。"""
        await _create(store)
        first = await manager.start(_spec(store, "sess_7e57e57e57e5", is_new=True))
        await first.task
        session = await store.get("sess_7e57e57e57e5")
        await store.add_turn("sess_7e57e57e57e5", "second turn")

        second = await manager.start(
            _spec(store, "sess_7e57e57e57e5", is_new=False, initial_seq=session.event_seq + 1)
        )
        await second.task
        assert second.log.first_seq == session.event_seq + 1
        # Attach with the snapshot watermark replays only the new turn
        # (sealed log → synchronous enumeration via replay).
        assert manager.attach("sess_7e57e57e57e5", since=session.event_seq) is not None
        entries = second.log.replay_after(session.event_seq)
        assert entries
        assert all(e.seq > session.event_seq for e in entries)
        assert entries[-1].payload["type"] == "complete"


class TestStartupSweep:
    async def test_sweep_marks_stale_running_interrupted(self, manager, store):
        await store.create("sess_7e57e57e0001", "a", "")
        await store.update("sess_7e57e57e0001", status="running")
        await store.create("sess_7e57e57e0002", "b", "")
        await store.update("sess_7e57e57e0002", status="completed")

        swept = await manager.sweep_stale_sessions()
        assert swept == 1
        stale = await store.get("sess_7e57e57e0001")
        done = await store.get("sess_7e57e57e0002")
        assert stale is not None and stale.status == "interrupted"
        assert stale.finished_at is not None
        assert done is not None and done.status == "completed"
