"""Unit tests for edit-resend session truncation (truncate_session_to_turn)."""

from __future__ import annotations

import json
import os
from dataclasses import replace

import pytest
from fastapi import HTTPException

from courtier.agent.api.models import SessionRecord, StepRecord, ThoughtRecord
from courtier.agent.api.services.session_service import (
    compute_turn_truncation,
    session_has_compacted,
    truncate_session_to_turn,
)
from courtier.agent.api.services.stream_service import (
    deserialize_messages,
    serialize_messages,
)
from courtier.agent.api.session_store import SessionStore
from courtier.agent.core.conversation_tree import ConversationTree
from courtier.agent.core.model import ToolCall
from courtier.agent.core.state import Message


def _three_turn_messages() -> tuple[Message, ...]:
    """A 3-turn history with loop-injected user-role noise mixed in."""
    return (
        Message(role="system", content="sys"),
        Message(role="user", content="问题一"),  # turn 0
        Message(role="user", content="提醒", source="reminder"),
        Message(
            role="assistant",
            content=None,
            tool_calls=(ToolCall(id="tc1", name="echo", arguments={}),),
        ),
        Message(role="tool", content="ok1", tool_call_id="tc1", name="echo"),
        Message(role="assistant", content="回答一"),
        Message(role="user", content="问题二"),  # turn 1
        Message(
            role="assistant",
            content=None,
            tool_calls=(ToolCall(id="tc2", name="echo", arguments={}),),
        ),
        Message(role="tool", content="ok2", tool_call_id="tc2", name="echo"),
        Message(role="assistant", content="回答二"),
        Message(role="user", content="问题三"),  # turn 2
        Message(role="user", content="提示", source="hint"),  # injected mid-turn-2
        Message(role="user", content="内联指令", source="inline"),
        Message(role="assistant", content="回答三"),
    )


def _make_session(turns: int = 3) -> SessionRecord:
    """Build a SessionRecord with *turns* turns of consistent bookkeeping."""
    messages = _three_turn_messages()
    assert turns == 3  # fixture is fixed at 3 turns
    steps = [
        StepRecord(index=i + 1, label=f"步骤{i + 1}", skill="", turn_index=t)
        for i, t in enumerate([0, 0, 1, 1, 1, 2])
    ]
    thoughts = [
        ThoughtRecord(
            id=i + 1,
            text=f"思考{i + 1}",
            turn=t + 1,
            timestamp=0.0,
            turn_index=t,
        )
        for i, t in enumerate([0, 1, 2])
    ]
    # Tree: root (system+user0) → end of turn 0 → end of turn 1 → end of turn 2.
    tree = ConversationTree.from_messages(messages[:2])
    n1 = tree.append_turn(tree.root_id, messages[:6])
    n2 = tree.append_turn(n1.node_id, messages[:10])
    n3 = tree.append_turn(n2.node_id, messages)
    return SessionRecord(
        id="sess_" + "a" * 12,
        task="问题一",
        file_id="",
        status="completed",
        steps=steps,
        thoughts=thoughts,
        conclusion="回答三",
        messages_json=serialize_messages(messages),
        context_state="",
        tree_json=json.dumps(tree.serialize(), ensure_ascii=False),
        current_node_id=n3.node_id,
        owner="admin",
        turn_messages=[
            {"text": "问题一", "timestamp": 1.0, "fileName": "a.docx"},
            {"text": "问题二", "timestamp": 2.0, "fileName": None},
            {"text": "问题三", "timestamp": 3.0, "fileName": None},
        ],
        turn_step_starts=[0, 2, 5],
        turn_conclusions=["回答一", "回答二", "回答三"],
    )


class TestComputeTurnTruncation:
    def test_middle_turn_messages(self):
        kwargs = compute_turn_truncation(_make_session(), 1)
        kept = deserialize_messages(kwargs["messages_json"])
        assert [m.content for m in kept] == ["sys", "问题一", "提醒", None, "ok1", "回答一"]
        # The reminder of the surviving turn is kept; injected noise of the
        # revoked turns is gone.
        assert all(m.role != "user" or m.source is None or m.content == "提醒" for m in kept)

    def test_middle_turn_bookkeeping(self):
        kwargs = compute_turn_truncation(_make_session(), 1)
        assert kwargs["turn_messages"] == [
            {"text": "问题一", "timestamp": 1.0, "fileName": "a.docx"}
        ]
        assert kwargs["turn_step_starts"] == [0]
        assert kwargs["turn_conclusions"] == ["回答一"]
        assert [s.index for s in kwargs["steps"]] == [1, 2]
        assert all(s.turn_index == 0 for s in kwargs["steps"])
        assert [t.text for t in kwargs["thoughts"]] == ["思考1"]
        # Session-level conclusion falls back to the last surviving turn.
        assert kwargs["conclusion"] == "回答一"
        # No compaction happened → context state resets to the new-session form.
        assert kwargs["context_state"] == ""
        # Happy-path record was completed → no status override.
        assert "status" not in kwargs

    def test_middle_turn_tree_pruned(self):
        session = _make_session()
        kwargs = compute_turn_truncation(session, 1)
        tree = ConversationTree.from_serialized(json.loads(kwargs["tree_json"]))
        assert len(tree.nodes) == 2  # root + end-of-turn-0
        current = tree.get(kwargs["current_node_id"])
        assert current is not None
        assert len(current.messages) == 6
        assert all(len(n.messages) <= 6 for n in tree.nodes.values())
        assert all(c in tree.nodes for n in tree.nodes.values() for c in n.children)

    def test_first_turn(self):
        kwargs = compute_turn_truncation(_make_session(), 0)
        kept = deserialize_messages(kwargs["messages_json"])
        assert [m.role for m in kept] == ["system"]
        assert kwargs["turn_messages"] == []
        assert kwargs["turn_step_starts"] == []
        assert kwargs["turn_conclusions"] == []
        assert kwargs["steps"] == []
        assert kwargs["thoughts"] == []
        assert kwargs["conclusion"] == ""
        # Root survives, trimmed to the truncated prefix.
        tree = ConversationTree.from_serialized(json.loads(kwargs["tree_json"]))
        assert tree.root_id in tree.nodes
        assert len(tree.nodes[tree.root_id].messages) == 1

    def test_last_turn(self):
        kwargs = compute_turn_truncation(_make_session(), 2)
        kept = deserialize_messages(kwargs["messages_json"])
        assert kept[-1].content == "回答二"
        assert kwargs["turn_conclusions"] == ["回答一", "回答二"]
        assert [s.index for s in kwargs["steps"]] == [1, 2, 3, 4, 5]

    def test_out_of_range(self):
        with pytest.raises(ValueError):
            compute_turn_truncation(_make_session(), 3)
        with pytest.raises(ValueError):
            compute_turn_truncation(_make_session(), -1)

    def test_mismatch_messages_vs_turns(self):
        session = _make_session()
        # Only 2 real user messages in the history, but 3 turn records.
        messages = tuple(m for m in _three_turn_messages() if m.content != "问题三")
        session = replace(session, messages_json=serialize_messages(messages))
        with pytest.raises(ValueError, match="不一致"):
            compute_turn_truncation(session, 2)

    def test_dangling_tool_calls_aligned(self):
        """A stopped mid-sequence tail must not leave dangling tool_calls."""
        messages = (
            Message(role="system", content="sys"),
            Message(role="user", content="问题一"),
            Message(role="assistant", content="回答一"),
            Message(role="user", content="问题二"),  # turn 1, stopped mid-sequence
            Message(
                role="assistant",
                content="部分回答",
                tool_calls=(ToolCall(id="tc9", name="echo", arguments={}),),
            ),
            Message(role="user", content="问题三"),  # turn 2
            Message(role="assistant", content="回答三"),
        )
        session = _make_session()
        session = replace(
            session,
            messages_json=serialize_messages(messages),
            turn_step_starts=[0, 2, 2],
            tree_json="",
            current_node_id=None,
        )
        # Truncating turn 2 leaves turn 1's dangling tail at the cut boundary.
        kwargs = compute_turn_truncation(session, 2)
        kept = deserialize_messages(kwargs["messages_json"])
        # The dangling assistant keeps its content but loses the unanswered call.
        assert kept[-1].content == "部分回答"
        assert not kept[-1].tool_calls

    def test_error_status_reset(self):
        session = _make_session()
        session = replace(session, status="error", error_detail="模型调用失败")
        kwargs = compute_turn_truncation(session, 1)
        assert kwargs["status"] == "completed"
        assert kwargs["error_detail"] is None


class TestSessionHasCompacted:
    def test_empty(self):
        assert session_has_compacted("") is False

    def test_fresh_state(self):
        state = json.dumps({"version": 1, "has_compacted": False, "compact_count": 0})
        assert session_has_compacted(state) is False

    def test_compacted(self):
        state = json.dumps({"version": 1, "has_compacted": True, "compact_count": 1})
        assert session_has_compacted(state) is True
        # compact_count alone is enough (defensive against partial payloads).
        state = json.dumps({"version": 1, "has_compacted": False, "compact_count": 2})
        assert session_has_compacted(state) is True

    def test_malformed(self):
        assert session_has_compacted("{not json") is False


async def _make_stored_session(store: SessionStore, owner: str = "admin") -> str:
    session = _make_session()
    await store.create(session.id, task=session.task, file_id="", owner=owner)
    await store.update(
        session.id,
        status=session.status,
        steps=session.steps,
        thoughts=session.thoughts,
        conclusion=session.conclusion,
        messages_json=session.messages_json,
        context_state=session.context_state,
        tree_json=session.tree_json,
        current_node_id=session.current_node_id,
        turn_messages=session.turn_messages,
        turn_step_starts=session.turn_step_starts,
        turn_conclusions=session.turn_conclusions,
    )
    return session.id


class TestArtifactSnapshotHistory:
    @pytest.mark.asyncio
    async def test_add_turn_records_snapshot_at_turn_start(self, tmp_path):
        store = SessionStore(str(tmp_path))
        session_id = "sess_" + "c" * 12
        await store.create(session_id, task="t0", file_id="", owner="admin")
        # Turn 0 starts empty: create() must not record a snapshot.
        await store.update(session_id, artifact_snapshot="snap0")
        await store.add_turn(session_id, "t1")
        session = await store.get(session_id)
        assert session.turn_artifact_snapshots == ["snap0"]
        await store.update(session_id, artifact_snapshot="snap1")
        await store.add_turn(session_id, "t2")
        session = await store.get(session_id)
        assert session.turn_artifact_snapshots == ["snap0", "snap1"]
        # Invariant: len(snapshots) == len(turn_messages) - 1.
        assert len(session.turn_artifact_snapshots) == len(session.turn_messages) - 1

    @pytest.mark.asyncio
    async def test_snapshot_history_survives_reload(self, tmp_path):
        store = SessionStore(str(tmp_path))
        session_id = "sess_" + "d" * 12
        await store.create(session_id, task="t0", file_id="", owner="admin")
        await store.update(session_id, artifact_snapshot="snap0")
        await store.add_turn(session_id, "t1")
        store._sessions.clear()
        reloaded = await store.get(session_id)
        assert reloaded is not None
        assert reloaded.turn_artifact_snapshots == ["snap0"]

    def test_truncate_rolls_back_to_turn_start_snapshot(self):
        session = replace(
            _make_session(),
            artifact_snapshot="snap2",
            turn_artifact_snapshots=["snap0", "snap1"],
        )
        kwargs = compute_turn_truncation(session, 2)
        assert kwargs["artifact_snapshot"] == "snap1"
        assert kwargs["turn_artifact_snapshots"] == ["snap0"]
        kwargs = compute_turn_truncation(session, 1)
        assert kwargs["artifact_snapshot"] == "snap0"
        assert kwargs["turn_artifact_snapshots"] == []

    def test_truncate_turn_zero_empties_store(self):
        session = replace(
            _make_session(),
            artifact_snapshot="snap2",
            turn_artifact_snapshots=["snap0", "snap1"],
        )
        kwargs = compute_turn_truncation(session, 0)
        assert kwargs["artifact_snapshot"] == ""
        assert kwargs["turn_artifact_snapshots"] == []
        # Single-turn legacy session (no history): revoking the only turn
        # still empties the store.
        legacy = replace(session, turn_artifact_snapshots=[])
        kwargs = compute_turn_truncation(legacy, 0)
        assert kwargs["artifact_snapshot"] == ""

    def test_truncate_legacy_keeps_snapshot_for_surviving_turns(self):
        # Sessions saved before per-turn snapshots existed: no rollback entry
        # for turn N ≥ 1 → keep the cumulative snapshot so surviving $refs
        # stay resolvable.
        session = replace(_make_session(), artifact_snapshot="latest", turn_artifact_snapshots=[])
        kwargs = compute_turn_truncation(session, 2)
        assert "artifact_snapshot" not in kwargs
        assert kwargs["turn_artifact_snapshots"] == []


class TestContextCompactedDetail:
    def test_detail_field(self):
        session = _make_session()
        assert session.to_detail_dict()["contextCompacted"] is False
        compacted = replace(
            session,
            context_state=json.dumps({"version": 1, "has_compacted": True, "compact_count": 1}),
        )
        assert compacted.to_detail_dict()["contextCompacted"] is True


class TestTruncateSessionToTurn:
    @pytest.mark.asyncio
    async def test_happy_path_persists(self, tmp_path):
        store = SessionStore(str(tmp_path))
        session_id = await _make_stored_session(store)
        updated = await truncate_session_to_turn(store, "admin", True, session_id, 1)
        assert len(updated.turn_messages) == 1
        # Reload from disk: the truncation must survive persistence.
        store._sessions.clear()
        reloaded = await store.get(session_id)
        assert reloaded is not None
        assert len(reloaded.turn_messages) == 1
        kept = deserialize_messages(reloaded.messages_json)
        assert kept[-1].content == "回答一"

    @pytest.mark.asyncio
    async def test_not_found(self, tmp_path):
        store = SessionStore(str(tmp_path))
        with pytest.raises(HTTPException) as exc_info:
            await truncate_session_to_turn(store, "admin", True, "sess_" + "b" * 12, 0)
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_ownership_enforced(self, tmp_path):
        store = SessionStore(str(tmp_path))
        session_id = await _make_stored_session(store, owner="admin")
        with pytest.raises(HTTPException) as exc_info:
            await truncate_session_to_turn(store, "user1", False, session_id, 1)
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_running_rejected(self, tmp_path):
        store = SessionStore(str(tmp_path))
        session_id = await _make_stored_session(store)
        await store.update(session_id, status="running")
        with pytest.raises(HTTPException) as exc_info:
            await truncate_session_to_turn(store, "admin", True, session_id, 1)
        assert exc_info.value.status_code == 409

    @pytest.mark.asyncio
    async def test_compacted_rejected(self, tmp_path):
        store = SessionStore(str(tmp_path))
        session_id = await _make_stored_session(store)
        await store.update(
            session_id,
            context_state=json.dumps({"version": 1, "has_compacted": True, "compact_count": 1}),
        )
        with pytest.raises(HTTPException) as exc_info:
            await truncate_session_to_turn(store, "admin", True, session_id, 1)
        assert exc_info.value.status_code == 409
        assert "已压缩" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_out_of_range_400(self, tmp_path):
        store = SessionStore(str(tmp_path))
        session_id = await _make_stored_session(store)
        with pytest.raises(HTTPException) as exc_info:
            await truncate_session_to_turn(store, "admin", True, session_id, 5)
        assert exc_info.value.status_code == 400


# -- Route-level tests for the editTurn SSE parameter -----------------------------

_TEST_ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "test-admin-password-for-pytest")


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    from courtier.agent.api.rate_limiter import limiter

    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture
async def app_client(tmp_path):
    from httpx import ASGITransport, AsyncClient

    from courtier.agent.api.app import create_app

    app = create_app(sessions_dir=str(tmp_path), start_plugins=False)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/auth/login",
                json={"username": "admin", "password": _TEST_ADMIN_PASSWORD},
            )
            assert resp.status_code == 200, f"Login failed: {resp.text}"
            client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
            yield app, client


async def _fake_build_agent(*args, **kwargs):
    from courtier.agent.agents.base import Agent
    from courtier.agent.testing import MockModelClient

    model = MockModelClient(tool_calls=[])
    return Agent(name="TestChat", role="Test role", tools=[], model=model), None, "test-model"


async def _run_turn(client, **params) -> None:
    """Drive one SSE run to completion with a mocked agent."""
    from unittest.mock import patch

    with patch("courtier.agent.api.routes.sessions.build_agent", new=_fake_build_agent):
        resp = await client.get("/api/sessions/run", params=params)
    assert resp.status_code == 200, resp.text


class TestEditTurnRoute:
    @pytest.mark.asyncio
    async def test_edit_resend_truncates_and_reruns(self, app_client):
        app, client = app_client
        await _run_turn(client, task="原始问题")
        sid = (await client.get("/api/sessions")).json()[0]["id"]
        await _run_turn(client, task="后续问题", sessionId=sid)
        detail = (await client.get(f"/api/sessions/{sid}")).json()
        assert [t["message"]["text"] for t in detail["turns"]] == ["原始问题", "后续问题"]

        await _run_turn(client, task="编辑后的问题", sessionId=sid, editTurn=0)

        detail = (await client.get(f"/api/sessions/{sid}")).json()
        assert [t["message"]["text"] for t in detail["turns"]] == ["编辑后的问题"]
        # Title follows the edited first turn.
        summary = (await client.get("/api/sessions")).json()[0]
        assert summary["task"] == "编辑后的问题"
        # The revoked turns are gone from the LLM context as well.
        store = app.state.session_store
        record = await store.get(sid)
        kept = deserialize_messages(record.messages_json)
        real_user = [m.content for m in kept if m.role == "user" and m.source is None]
        assert real_user == ["编辑后的问题"]

    @pytest.mark.asyncio
    async def test_edit_keeps_original_file_chip(self, app_client):
        app, client = app_client
        upload = await client.post(
            "/api/files",
            files={"file": ("报告.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 100, "image/png")},
        )
        assert upload.status_code == 200, upload.text
        file_id = upload.json()["fileId"]
        await _run_turn(client, task="审计这份文件", fileId=file_id)
        sid = (await client.get("/api/sessions")).json()[0]["id"]

        detail = (await client.get(f"/api/sessions/{sid}")).json()
        assert detail["turns"][0]["message"]["fileId"] == file_id

        # Edit-resend without re-uploading: the client re-sends the original
        # fileId so the reference survives truncation.
        await _run_turn(client, task="重新审计", sessionId=sid, editTurn=0, fileId=file_id)

        detail = (await client.get(f"/api/sessions/{sid}")).json()
        assert len(detail["turns"]) == 1
        assert detail["turns"][0]["message"]["fileName"] == "报告.png"
        assert detail["turns"][0]["message"]["fileId"] == file_id
        record = await app.state.session_store.get(sid)
        assert record.file_id == file_id

    @pytest.mark.asyncio
    async def test_edit_turn_requires_session(self, app_client):
        _app, client = app_client
        resp = await client.get("/api/sessions/run", params={"task": "x", "editTurn": 0})
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_edit_turn_out_of_range(self, app_client):
        _app, client = app_client
        await _run_turn(client, task="唯一一轮")
        sid = (await client.get("/api/sessions")).json()[0]["id"]
        resp = await client.get(
            "/api/sessions/run", params={"task": "x", "sessionId": sid, "editTurn": 9}
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_edit_turn_running_rejected_by_status(self, app_client):
        app, client = app_client
        await _run_turn(client, task="跑过一轮")
        sid = (await client.get("/api/sessions")).json()[0]["id"]
        await app.state.session_store.update(sid, status="running")
        resp = await client.get(
            "/api/sessions/run", params={"task": "x", "sessionId": sid, "editTurn": 0}
        )
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_edit_turn_running_rejected_by_active_run(self, app_client):
        app, client = app_client
        await _run_turn(client, task="跑过一轮")
        sid = (await client.get("/api/sessions")).json()[0]["id"]
        # Simulate a live run registered in the RunManager (the persisted
        # status can lag the in-memory runner).
        from courtier.agent.api.services.run_event_log import RunEventLog
        from courtier.agent.api.services.run_manager import AgentRun
        from courtier.agent.core.event_bus import EventBus

        app.state.run_manager._runs[sid] = AgentRun(sid, "u", RunEventLog(), EventBus())
        resp = await client.get(
            "/api/sessions/run", params={"task": "x", "sessionId": sid, "editTurn": 0}
        )
        assert resp.status_code == 409
        app.state.run_manager._runs.pop(sid, None)

    @pytest.mark.asyncio
    async def test_edit_turn_compacted_rejected(self, app_client):
        app, client = app_client
        await _run_turn(client, task="跑过一轮")
        sid = (await client.get("/api/sessions")).json()[0]["id"]
        await app.state.session_store.update(
            sid,
            context_state=json.dumps({"version": 1, "has_compacted": True, "compact_count": 1}),
        )
        resp = await client.get(
            "/api/sessions/run", params={"task": "x", "sessionId": sid, "editTurn": 0}
        )
        assert resp.status_code == 409
        # Detail payload exposes the flag for the frontend to hide the entry.
        detail = (await client.get(f"/api/sessions/{sid}")).json()
        assert detail["contextCompacted"] is True
