"""Tests for SessionStore."""

import json
import random
import tempfile

import pytest

from courtier.agent.api.models import StepRecord, SubagentRunRecord, ThoughtRecord, ToolInfo
from courtier.agent.api.session_store import SessionStore


def _rid() -> str:
    """Generate a random 12-char hex id for test session ids."""
    return "".join(random.choices("abcdef0123456789", k=12))


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as d:
        yield SessionStore(d)


@pytest.mark.asyncio
class TestSessionStore:
    async def test_create_and_get(self, store):
        s = await store.create(
            "sess_000000000001", "audit", "file_abc12345", "test-model", owner="alice"
        )
        assert s.id == "sess_000000000001"
        assert s.status == "running"

        loaded = await store.get("sess_000000000001")
        assert loaded is not None
        assert loaded.id == "sess_000000000001"
        assert loaded.task == "audit"

    async def test_get_nonexistent(self, store):
        assert await store.get("nonexistent") is None

    async def test_list_all_empty(self, store):
        assert await store.list_all() == []

    async def test_list_all(self, store):
        await store.create(
            "sess_aaaaaaaaaaaa",
            "task a",
            "file_aaaa1111",
            created_at=100.0,
            owner="alice",
        )
        await store.create(
            "sess_bbbbbbbbbbbb",
            "task b",
            "file_bbbb2222",
            created_at=200.0,
            owner="alice",
        )
        await store.create(
            "sess_cccccccccccc",
            "task c",
            "file_cccc3333",
            created_at=300.0,
            owner="alice",
        )

        sessions = await store.list_all(current_user="alice")
        assert len(sessions) == 3
        # Most recent first
        assert sessions[0]["id"] == "sess_cccccccccccc"
        assert sessions[1]["id"] == "sess_bbbbbbbbbbbb"
        assert sessions[2]["id"] == "sess_aaaaaaaaaaaa"

    async def test_list_all_pinned_first(self, store):
        await store.create("sess_aaaaaaaaaaaa", "task a", "file_a", created_at=100.0, owner="alice")
        await store.create("sess_bbbbbbbbbbbb", "task b", "file_b", created_at=200.0, owner="alice")
        await store.create("sess_cccccccccccc", "task c", "file_c", created_at=300.0, owner="alice")
        await store.update("sess_aaaaaaaaaaaa", pinned=True)

        sessions = await store.list_all(current_user="alice")
        # Pinned first (newest among pinned), then most recent first
        assert [s["id"] for s in sessions] == [
            "sess_aaaaaaaaaaaa",
            "sess_cccccccccccc",
            "sess_bbbbbbbbbbbb",
        ]
        assert sessions[0]["pinned"] is True
        assert sessions[1]["pinned"] is False

    async def test_persist_and_reload_pinned(self, tmp_path):
        store = SessionStore(str(tmp_path))
        await store.create("sess_000000000001", "task", "file_abc12345", owner="alice")
        await store.update("sess_000000000001", pinned=True)

        reloaded = SessionStore(str(tmp_path))
        s = await reloaded.get("sess_000000000001")
        assert s is not None
        assert s.pinned is True

    async def test_delete(self, store):
        await store.create("sess_000000000001", "task", "file_abc12345", owner="alice")
        assert await store.delete("sess_000000000001") is True
        assert await store.get("sess_000000000001") is None

    async def test_delete_nonexistent(self, store):
        assert await store.delete("nonexistent") is False

    async def test_update(self, store):
        await store.create("sess_000000000001", "task", "file_abc12345", owner="alice")
        await store.update("sess_000000000001", status="completed", tokens_in=100)
        s = await store.get("sess_000000000001")
        assert s is not None
        assert s.status == "completed"
        assert s.tokens_in == 100

    async def test_add_step(self, store):
        await store.create("sess_000000000001", "task", "file_abc12345", owner="alice")
        step = StepRecord(index=1, label="check_format", skill="格式检查")
        await store.add_step("sess_000000000001", step)

        s = await store.get("sess_000000000001")
        assert s is not None
        assert len(s.steps) == 1
        assert s.steps[0].label == "check_format"

    async def test_add_thought(self, store):
        await store.create("sess_000000000001", "task", "file_abc12345", owner="alice")
        thought = ThoughtRecord(id=1, text="thinking", turn=0, timestamp=123.0)
        await store.add_thought("sess_000000000001", thought)

        s = await store.get("sess_000000000001")
        assert s is not None
        assert len(s.thoughts) == 1
        assert s.thoughts[0].text == "thinking"

    async def test_add_tool_info(self, store):
        await store.create("sess_000000000001", "task", "file_abc12345", owner="alice")
        step = StepRecord(index=1, label="t1", skill="s1")
        await store.add_step("sess_000000000001", step)

        ti = ToolInfo(
            name="check_format",
            skill="格式检查",
            status="done",
            duration=0.5,
            summary="ok",
        )
        await store.add_tool_info("sess_000000000001", ti)

        s = await store.get("sess_000000000001")
        assert s is not None
        assert len(s.steps[0].tools) == 1
        assert s.steps[0].tools[0].name == "check_format"

    async def test_set_verdict(self, store):
        await store.create("sess_000000000001", "task", "file_abc12345", owner="alice")
        step = StepRecord(index=1, label="t1", skill="s1")
        await store.add_step("sess_000000000001", step)

        await store.set_verdict("sess_000000000001", "格式存在问题")
        s = await store.get("sess_000000000001")
        assert s is not None
        assert s.steps[0].verdict == "格式存在问题"

    async def test_persist_and_reload(self, store):
        await store.create(
            "sess_000000000001",
            "task",
            "file_abc12345",
            model_name="m",
            created_at=100.0,
            owner="alice",
        )
        step = StepRecord(index=1, label="t1", skill="s1")
        await store.add_step("sess_000000000001", step)
        ti = ToolInfo(
            name="check_format",
            skill="格式检查",
            status="warning",
            duration=0.5,
            summary="3 issues",
        )
        await store.add_tool_info("sess_000000000001", ti)

        # Reload from disk
        loaded = await store.get("sess_000000000001")
        assert loaded is not None
        assert loaded.task == "task"
        assert len(loaded.steps) == 1
        assert loaded.steps[0].tools[0].name == "check_format"
        assert loaded.steps[0].tools[0].status == "warning"

    async def test_persist_and_reload_tool_call_classification(self, tmp_path):
        store = SessionStore(str(tmp_path))
        await store.create(
            "sess_000000000001",
            "task",
            "file_abc12345",
            model_name="m",
            created_at=100.0,
            owner="alice",
        )
        await store.add_step(
            "sess_000000000001",
            StepRecord(index=1, label="run_format_auditor", skill="s1"),
        )
        await store.add_tool_info(
            "sess_000000000001",
            ToolInfo(
                name="run_format_auditor",
                skill="format_auditor",
                status="done",
                duration=0.5,
                summary="ok",
                call_kind="subagent_run",
                call_scope="parent",
                subagent_name="format_auditor",
            ),
        )

        reloaded_store = SessionStore(str(tmp_path))
        loaded = await reloaded_store.get("sess_000000000001")

        assert loaded is not None
        tool = loaded.steps[0].tools[0]
        assert tool.call_kind == "subagent_run"
        assert tool.call_scope == "parent"
        assert tool.subagent_name == "format_auditor"
        assert loaded.to_detail_dict()["steps"][0]["tools"][0]["callKind"] == "subagent_run"

    async def test_persist_and_reload_with_display_name(self, tmp_path):
        store = SessionStore(str(tmp_path))
        await store.create(
            "sess_000000000001",
            "task",
            "file_abc12345",
            model_name="m",
            created_at=100.0,
            owner="alice",
        )
        await store.add_step(
            "sess_000000000001",
            StepRecord(index=1, label="parse_document", skill="parse"),
        )
        await store.add_tool_info(
            "sess_000000000001",
            ToolInfo(
                name="parse_document",
                skill="parse",
                status="done",
                duration=0.3,
                summary="parsed",
                display_name="文档解析",
            ),
        )

        reloaded_store = SessionStore(str(tmp_path))
        loaded = await reloaded_store.get("sess_000000000001")

        assert loaded is not None
        tool = loaded.steps[0].tools[0]
        assert tool.display_name == "文档解析"
        assert loaded.to_detail_dict()["steps"][0]["tools"][0]["displayName"] == "文档解析"

    async def test_load_legacy_tool_without_display_name_defaults_to_none(self, tmp_path):
        store = SessionStore(str(tmp_path))
        path = tmp_path / "sess_1e9ac71e9ac7.json"
        path.write_text(
            """
            {
              "id": "sess_1e9ac71e9ac7",
              "task": "legacy task",
              "status": "completed",
              "steps": [
                {
                  "index": 1,
                  "label": "parse_document",
                  "skill": "parse",
                  "tools": [
                    {
                      "name": "parse_document",
                      "skill": "parse",
                      "status": "done",
                      "duration": 0.2,
                      "summary": "ok"
                    }
                  ],
                  "verdict": ""
                }
              ],
              "thoughts": [],
              "stats": {"tokensIn": 0, "tokensOut": 0, "elapsed": 0.0},
              "conclusion": "",
              "createdAt": 100.0
            }
            """.strip(),
            encoding="utf-8",
        )

        loaded = await store.get("sess_1e9ac71e9ac7")

        assert loaded is not None
        tool = loaded.steps[0].tools[0]
        assert tool.display_name is None

    async def test_load_legacy_tool_without_call_classification_defaults_to_parent_tool(
        self, tmp_path
    ):
        store = SessionStore(str(tmp_path))
        path = tmp_path / "sess_1e9ac71e9ac7.json"
        path.write_text(
            """
            {
              "id": "sess_1e9ac71e9ac7",
              "task": "legacy task",
              "status": "completed",
              "steps": [
                {
                  "index": 1,
                  "label": "parse_document",
                  "skill": "parse",
                  "tools": [
                    {
                      "name": "parse_document",
                      "skill": "parse",
                      "status": "done",
                      "duration": 0.2,
                      "summary": "ok"
                    }
                  ],
                  "verdict": ""
                }
              ],
              "thoughts": [],
              "stats": {"tokensIn": 0, "tokensOut": 0, "elapsed": 0.0},
              "conclusion": "",
              "createdAt": 100.0
            }
            """.strip(),
            encoding="utf-8",
        )

        loaded = await store.get("sess_1e9ac71e9ac7")

        assert loaded is not None
        tool = loaded.steps[0].tools[0]
        assert tool.call_kind == "tool"
        assert tool.call_scope == "parent"
        assert tool.subagent_name is None

    @pytest.mark.asyncio
    async def test_persist_and_reload_preserves_handle_ids(self, store: SessionStore):
        """handleId / parentHandleId / parentSubagentName survive round-trip."""
        sid = f"sess_{_rid()}"
        await store.create(sid, "audit", "/tmp/f.docx")

        from courtier.agent.api.models import StepRecord, ToolInfo

        tool = ToolInfo(
            name="run_auditor",
            skill="audit",
            status="done",
            duration=1.0,
            summary="ok",
            call_kind="subagent_run",
            call_scope="parent",
            subagent_name="format_auditor",
            handle_id="hdl_abc",
            parent_handle_id="hdl_parent",
            parent_subagent_name="parent_agent",
        )
        step = StepRecord(index=1, label="run_auditor", skill="audit", tools=[tool])
        await store.add_step(sid, step)

        loaded = await store.get(sid)
        assert loaded is not None
        lt = loaded.steps[0].tools[0]
        assert lt.handle_id == "hdl_abc"
        assert lt.parent_handle_id == "hdl_parent"
        assert lt.parent_subagent_name == "parent_agent"

    @pytest.mark.asyncio
    async def test_persist_and_reload_preserves_subagents(self, store: SessionStore):
        """Sub-agent trees survive round-trip via finalize_step."""
        sid = f"sess_{_rid()}"
        await store.create(sid, "audit", "/tmp/f.docx")

        from courtier.agent.api.models import (
            StepRecord,
            SubagentRunRecord,
            SubagentThoughtRecord,
        )

        sa = SubagentRunRecord(
            name="format_auditor",
            handle_id="hdl_root",
            status="completed",
            conclusion="审核通过",
            thoughts=[SubagentThoughtRecord(id=1, text="检查中...")],
        )
        step = StepRecord(
            index=1,
            label="run_format_auditor",
            skill="format_auditor",
            turn_index=1,
            start_segment_index=5,
        )
        await store.add_step(sid, step)
        await store.finalize_step(
            sid,
            step_index=1,
            subagents=[sa],
            end_segment_index=12,
        )

        loaded = await store.get(sid)
        assert loaded is not None
        ls = loaded.steps[0]
        assert ls.turn_index == 1
        assert ls.start_segment_index == 5
        assert ls.end_segment_index == 12
        assert len(ls.subagents) == 1
        assert ls.subagents[0].name == "format_auditor"
        assert ls.subagents[0].conclusion == "审核通过"
        assert len(ls.subagents[0].thoughts) == 1
        assert ls.subagents[0].thoughts[0].text == "检查中..."

    @pytest.mark.asyncio
    async def test_persist_and_reload_preserves_thought_metadata(self, store: SessionStore):
        """Thought step_index and turn_index survive round-trip."""
        sid = f"sess_{_rid()}"
        await store.create(sid, "audit", "/tmp/f.docx")

        from courtier.agent.api.models import ThoughtRecord

        t = ThoughtRecord(
            id=1,
            text="thinking...",
            turn=1,
            timestamp=123.0,
            step_index=3,
            turn_index=2,
        )
        await store.add_thought(sid, t)

        loaded = await store.get(sid)
        assert loaded is not None
        lt = loaded.thoughts[0]
        assert lt.step_index == 3
        assert lt.turn_index == 2

    @pytest.mark.asyncio
    async def test_load_legacy_session_without_new_fields(self, store: SessionStore):
        """Loading a session persisted before the subagent schema update does not crash."""
        import json
        from pathlib import Path

        legacy = {
            "id": "sess_legacy01",
            "task": "old audit",
            "file_id": "/tmp/old.docx",
            "file_name": "old.docx",
            "status": "completed",
            "createdAt": 1748600000.0,
            "steps": [
                {
                    "index": 1,
                    "numeral": "壹",
                    "label": "check_format",
                    "skill": "format",
                    "tools": [
                        {
                            "name": "check_format",
                            "skill": "format",
                            "status": "done",
                            "duration": 1.0,
                            "summary": "ok",
                        }
                    ],
                    "verdict": "",
                }
            ],
            "thoughts": [
                {
                    "id": 1,
                    "text": "old thought",
                    "turn": 1,
                    "timestamp": 123.0,
                    "segmentIndex": 0,
                    "segmentType": "observe",
                }
            ],
            "stats": {"tokensIn": 10, "tokensOut": 5, "elapsed": 1.0},
            "conclusion": "done",
            "owner": "test",
            "turn_messages": [],
            "turn_step_starts": [],
        }
        file_path = Path(store._dir) / "sess_legacy01.json"
        file_path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")

        loaded = store._load("sess_legacy01")
        assert loaded is not None
        assert loaded.steps[0].subagents == []
        assert loaded.steps[0].turn_index == 0
        assert loaded.steps[0].start_segment_index == 0
        assert loaded.steps[0].end_segment_index is None
        assert loaded.thoughts[0].step_index is None
        assert loaded.thoughts[0].turn_index == 0


class TestStepMetadataPreservation:
    """Regression tests for StepRecord metadata survival across updates."""

    @pytest.mark.asyncio
    async def test_add_tool_info_preserves_turn_index_subagents_and_segment(self, store):
        sid = f"sess_{_rid()}"
        await store.create(sid, "audit", "/tmp/f.docx")

        step = StepRecord(
            index=1,
            label="run",
            skill="audit",
            turn_index=2,
            start_segment_index=5,
            end_segment_index=10,
            subagents=[SubagentRunRecord(name="sa", handle_id="h1")],
        )
        await store.add_step(sid, step)
        await store.add_tool_info(
            sid,
            ToolInfo(
                name="t1",
                skill="",
                status="done",
                duration=0.1,
                summary="ok",
                id="tool-1",
            ),
        )

        loaded = await store.get(sid)
        assert loaded is not None
        assert loaded.steps[0].turn_index == 2
        assert loaded.steps[0].start_segment_index == 5
        assert loaded.steps[0].end_segment_index == 10
        assert len(loaded.steps[0].subagents) == 1
        assert loaded.steps[0].tools[0].id == "tool-1"

    @pytest.mark.asyncio
    async def test_set_verdict_preserves_turn_index_subagents_and_segment(self, store):
        sid = f"sess_{_rid()}"
        await store.create(sid, "audit", "/tmp/f.docx")

        step = StepRecord(
            index=1,
            label="run",
            skill="audit",
            turn_index=2,
            start_segment_index=5,
            end_segment_index=10,
            subagents=[SubagentRunRecord(name="sa", handle_id="h1")],
        )
        await store.add_step(sid, step)
        await store.set_verdict(sid, "verdict text")

        loaded = await store.get(sid)
        assert loaded is not None
        assert loaded.steps[0].turn_index == 2
        assert loaded.steps[0].start_segment_index == 5
        assert loaded.steps[0].end_segment_index == 10
        assert len(loaded.steps[0].subagents) == 1
        assert loaded.steps[0].verdict == "verdict text"


class TestTurnConclusionPersistence:
    """Per-turn conclusions must survive across multiple turns and reloads."""

    @pytest.mark.asyncio
    async def test_finalize_turn_conclusion_appends_per_turn(self, store):
        sid = f"sess_{_rid()}"
        await store.create(sid, "turn 1", "file_1", created_at=100.0, owner="alice")
        await store.finalize_turn_conclusion(sid, "结论一")

        loaded = await store.get(sid)
        assert loaded is not None
        assert loaded.turn_conclusions == ["结论一"]
        assert loaded.to_detail_dict()["turns"][0]["conclusion"] == "结论一"

        await store.add_turn(sid, "turn 2")
        await store.finalize_turn_conclusion(sid, "结论二")

        loaded = await store.get(sid)
        assert loaded is not None
        assert loaded.turn_conclusions == ["结论一", "结论二"]
        turns = loaded.to_detail_dict()["turns"]
        assert len(turns) == 2
        assert turns[0]["conclusion"] == "结论一"
        assert turns[1]["conclusion"] == "结论二"

    @pytest.mark.asyncio
    async def test_finalize_turn_conclusion_replaces_duplicate_complete(self, store):
        sid = f"sess_{_rid()}"
        await store.create(sid, "turn 1", "file_1", owner="alice")
        await store.finalize_turn_conclusion(sid, "结论一")
        await store.finalize_turn_conclusion(sid, "结论一修正")

        loaded = await store.get(sid)
        assert loaded is not None
        assert loaded.turn_conclusions == ["结论一修正"]

    @pytest.mark.asyncio
    async def test_persist_and_reload_preserves_turn_conclusions(self, store):
        sid = f"sess_{_rid()}"
        await store.create(sid, "turn 1", "file_1", created_at=100.0, owner="alice")
        await store.add_turn(sid, "turn 2")
        await store.finalize_turn_conclusion(sid, "结论一")
        await store.finalize_turn_conclusion(sid, "结论二")

        # Reload from a fresh store instance using the same directory
        reloaded_store = SessionStore(str(store._dir))
        loaded = await reloaded_store.get(sid)
        assert loaded is not None
        assert loaded.turn_conclusions == ["结论一", "结论二"]

    @pytest.mark.asyncio
    async def test_load_legacy_session_without_turn_conclusions(self, store):
        """Legacy sessions without turn_conclusions still expose the session conclusion."""
        import json
        from pathlib import Path

        legacy = {
            "id": "sess_1e9ac71e9ac7",
            "task": "old audit",
            "file_id": "/tmp/old.docx",
            "file_name": "old.docx",
            "status": "completed",
            "createdAt": 1748600000.0,
            "steps": [
                {
                    "index": 1,
                    "numeral": "壹",
                    "label": "check_format",
                    "skill": "format",
                    "tools": [
                        {
                            "name": "check_format",
                            "skill": "format",
                            "status": "done",
                            "duration": 1.0,
                            "summary": "ok",
                        }
                    ],
                    "verdict": "",
                }
            ],
            "thoughts": [],
            "stats": {"tokensIn": 10, "tokensOut": 5, "elapsed": 1.0},
            "conclusion": "最终结论",
            "owner": "test",
            "turn_messages": [
                {"text": "turn 1", "timestamp": 100.0},
                {"text": "turn 2", "timestamp": 200.0},
            ],
            "turn_step_starts": [0, 1],
        }
        file_path = Path(store._dir) / "sess_1e9ac71e9ac7.json"
        file_path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")

        loaded = await store.get("sess_1e9ac71e9ac7")
        assert loaded is not None
        turns = loaded.to_detail_dict()["turns"]
        assert len(turns) == 2
        assert turns[0]["conclusion"] == ""
        assert turns[1]["conclusion"] == "最终结论"


@pytest.mark.asyncio
class TestIssueCountsPersistence:
    """issue_counts 的持久化与恢复（含旧会话兼容）。"""

    async def test_persist_and_reload_issue_counts(self, tmp_path):
        store = SessionStore(str(tmp_path))
        await store.create(
            "sess_000000000001",
            "task",
            "file_abc12345",
            created_at=100.0,
        )
        await store.add_step(
            "sess_000000000001",
            StepRecord(index=1, label="check_format", skill="format_audit"),
        )
        counts = {"err": 3, "warn": 0, "ok": 1, "unchecked": 2}
        await store.add_tool_info(
            "sess_000000000001",
            ToolInfo(
                name="check_format",
                skill="format_audit",
                status="error",
                duration=0.5,
                summary="3 项错误",
                issue_counts=counts,
            ),
        )

        reloaded_store = SessionStore(str(tmp_path))
        loaded = await reloaded_store.get("sess_000000000001")

        assert loaded is not None
        tool = loaded.steps[0].tools[0]
        assert tool.issue_counts == counts
        assert loaded.to_detail_dict()["steps"][0]["tools"][0]["issueCounts"] == counts

    async def test_load_legacy_tool_without_issue_counts_defaults_to_none(self, tmp_path):
        path = tmp_path / "sess_1e9ac71e9ac7.json"
        path.write_text(
            json.dumps(
                {
                    "id": "sess_1e9ac71e9ac7",
                    "task": "legacy task",
                    "status": "completed",
                    "steps": [
                        {
                            "index": 1,
                            "label": "check_format",
                            "skill": "format_audit",
                            "tools": [
                                {
                                    "name": "check_format",
                                    "skill": "format_audit",
                                    "status": "warning",
                                    "duration": 0.5,
                                    "summary": "1 项警告",
                                }
                            ],
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        store = SessionStore(str(tmp_path))
        loaded = await store.get("sess_1e9ac71e9ac7")

        assert loaded is not None
        tool = loaded.steps[0].tools[0]
        assert tool.issue_counts is None
        # Re-serialization must not invent the field for legacy tools.
        assert "issueCounts" not in loaded.to_detail_dict()["steps"][0]["tools"][0]

    async def test_persist_and_reload_subagent_tool_issue_counts(self, tmp_path):
        from courtier.agent.api.models import SubagentRunRecord, SubagentToolRecord

        store = SessionStore(str(tmp_path))
        await store.create(
            "sess_000000000001",
            "task",
            "file_abc12345",
            created_at=100.0,
        )
        await store.add_step(
            "sess_000000000001",
            StepRecord(index=1, label="run_format_auditor", skill="format_audit"),
        )
        counts = {"err": 1, "warn": 2, "ok": 7}
        await store.finalize_step(
            "sess_000000000001",
            1,
            subagents=[
                SubagentRunRecord(
                    name="format_auditor",
                    handle_id="hdl_1",
                    status="completed",
                    tools=[
                        SubagentToolRecord(
                            name="check_format",
                            status="done",
                            duration=0.5,
                            summary="checked",
                            issue_counts=counts,
                        )
                    ],
                )
            ],
        )

        reloaded_store = SessionStore(str(tmp_path))
        loaded = await reloaded_store.get("sess_000000000001")

        assert loaded is not None
        tool = loaded.steps[0].subagents[0].tools[0]
        assert tool.issue_counts == counts
        detail = loaded.to_detail_dict()
        assert detail["steps"][0]["subagents"][0]["tools"][0]["issueCounts"] == counts
