"""Tests for API data models."""

import pytest

from courtier.agent.api.models import (
    SessionRecord,
    StepRecord,
    ThoughtRecord,
    ToolInfo,
    _numeral,
    normalize_tool_call_classification,
)


class TestNumeral:
    def test_known(self):
        assert _numeral(1) == "壹"
        assert _numeral(2) == "贰"
        assert _numeral(3) == "叁"
        assert _numeral(5) == "伍"
        assert _numeral(10) == "拾"

    def test_unknown_falls_back_to_str(self):
        assert _numeral(11) == "11"
        assert _numeral(0) == "0"


class TestThoughtRecord:
    def test_creation(self):
        t = ThoughtRecord(id=1, text="hello", turn=0, timestamp=123.0)
        assert t.id == 1
        assert t.text == "hello"
        assert t.turn == 0
        assert t.timestamp == 123.0

    def test_immutable(self):
        t = ThoughtRecord(id=1, text="hello", turn=0, timestamp=123.0)
        with pytest.raises(Exception):
            t.id = 2  # type: ignore


class TestToolInfo:
    def test_creation(self):
        ti = ToolInfo(
            name="check_format",
            skill="格式检查",
            status="done",
            duration=1.2,
            summary="完成",
            detail={"type": "structured", "data": {"errors": 2}},
        )
        assert ti.name == "check_format"
        assert ti.status == "done"
        assert ti.detail["data"]["errors"] == 2

    def test_detail_none(self):
        ti = ToolInfo(name="echo", skill="", status="done", duration=0.0, summary="ok")
        assert ti.detail is None

    def test_to_dict_includes_default_call_classification(self):
        ti = ToolInfo(
            name="parse_document",
            skill="parse",
            status="done",
            duration=0.1,
            summary="ok",
        )

        data = ti.to_dict()

        assert data["name"] == "parse_document"
        assert data["callKind"] == "tool"
        assert data["callScope"] == "parent"
        assert data["subagentName"] is None

    def test_to_dict_includes_subagent_run_classification(self):
        ti = ToolInfo(
            name="run_format_auditor",
            skill="format_auditor",
            status="done",
            duration=1.3,
            summary="done",
            call_kind="subagent_run",
            call_scope="parent",
            subagent_name="format_auditor",
        )

        data = ti.to_dict()

        assert data["callKind"] == "subagent_run"
        assert data["callScope"] == "parent"
        assert data["subagentName"] == "format_auditor"

    def test_normalize_tool_call_classification_falls_back_on_invalid_values(self):
        normalized = normalize_tool_call_classification(
            {
                "call_kind": "not-valid",
                "call_scope": "also-invalid",
                "subagent_name": 123,
            }
        )

        assert normalized == {
            "call_kind": "tool",
            "call_scope": "parent",
            "subagent_name": None,
        }

    def test_normalize_tool_call_classification_falls_back_on_list_call_kind(self):
        normalized = normalize_tool_call_classification(
            {
                "call_kind": ["subagent_run"],
                "call_scope": "subagent",
                "subagent_name": "format_auditor",
            }
        )

        assert normalized == {
            "call_kind": "tool",
            "call_scope": "subagent",
            "subagent_name": "format_auditor",
        }

    def test_normalize_tool_call_classification_falls_back_on_dict_call_scope(self):
        normalized = normalize_tool_call_classification(
            {
                "call_kind": "subagent_run",
                "call_scope": {"scope": "subagent"},
                "subagent_name": "format_auditor",
            }
        )

        assert normalized == {
            "call_kind": "subagent_run",
            "call_scope": "parent",
            "subagent_name": "format_auditor",
        }

    def test_to_dict_includes_display_name_when_provided(self):
        ti = ToolInfo(
            name="parse_document",
            skill="parse",
            status="done",
            duration=0.1,
            summary="ok",
            display_name="文档解析",
        )

        data = ti.to_dict()

        assert data["name"] == "parse_document"
        assert data["displayName"] == "文档解析"

    def test_to_dict_display_name_is_null_when_not_provided(self):
        ti = ToolInfo(
            name="parse_document",
            skill="parse",
            status="done",
            duration=0.1,
            summary="ok",
        )

        data = ti.to_dict()

        assert data["name"] == "parse_document"
        assert data["displayName"] is None

    def test_creation_without_display_name_defaults_to_none(self):
        ti = ToolInfo(name="echo", skill="", status="done", duration=0.0, summary="ok")
        assert ti.display_name is None


class TestStepRecord:
    def test_creation(self):
        s = StepRecord(index=1, label="check_format", skill="格式检查")
        assert s.numeral == "壹"
        assert s.verdict == ""
        assert s.tools == []

    def test_with_tools(self):
        tool = ToolInfo(
            name="check_format",
            skill="格式检查",
            status="warning",
            duration=0.5,
            summary="3 issues",
        )
        s = StepRecord(
            index=2,
            label="check_format, check_grammar",
            skill="格式检查",
            tools=[tool],
            verdict="有问题",
        )
        assert s.numeral == "贰"
        assert len(s.tools) == 1
        assert s.verdict == "有问题"

    def test_immutable(self):
        s = StepRecord(index=1, label="t1", skill="s1")
        with pytest.raises(Exception):
            s.index = 2  # type: ignore


class TestSessionRecord:
    def test_creation(self):
        s = SessionRecord(id="sess_abc", task="audit", file_id="/tmp/test.docx")
        assert s.status == "running"
        assert s.steps == []
        assert s.tokens_in == 0

    def test_to_summary_dict(self):
        s = SessionRecord(
            id="sess_abc",
            task="audit",
            file_id="/tmp/test.docx",
            status="completed",
            created_at=1748600000.0,
        )
        summary = s.to_summary_dict()
        assert summary["id"] == "sess_abc"
        assert summary["task"] == "audit"
        assert summary["status"] == "completed"
        assert summary["stepCount"] == 0
        assert summary["toolCount"] == 0
        assert summary["issueCount"] == 0

    def test_to_summary_with_issues(self):
        tool_ok = ToolInfo(
            name="t1", skill="s1", status="done", duration=0.1, summary="ok"
        )
        tool_warn = ToolInfo(
            name="t2", skill="s2", status="warning", duration=0.2, summary="warn"
        )
        step = StepRecord(
            index=1, label="t1, t2", skill="s1", tools=[tool_ok, tool_warn], verdict=""
        )
        s = SessionRecord(id="sess_abc", task="audit", file_id="/tmp/test.docx")
        s.steps.append(step)
        summary = s.to_summary_dict()
        assert summary["toolCount"] == 2
        assert summary["issueCount"] == 1

    def test_to_detail_dict(self):
        tool = ToolInfo(
            name="check_format",
            skill="格式检查",
            status="warning",
            duration=1.2,
            summary="发现 2 处问题",
            detail={"type": "structured", "data": {"errors": 2}},
        )
        step = StepRecord(
            index=1,
            label="check_format",
            skill="格式检查",
            tools=[tool],
            verdict="格式有问题",
        )
        thought = ThoughtRecord(id=1, text="thinking...", turn=1, timestamp=123.0)
        s = SessionRecord(
            id="sess_abc",
            task="audit",
            file_id="/tmp/test.docx",
            model_name="deepseek-v4",
            status="completed",
            tokens_in=100,
            tokens_out=50,
            created_at=1748600000.0,
            finished_at=1748600010.0,
        )
        s.steps.append(step)
        s.thoughts.append(thought)

        d = s.to_detail_dict()
        assert d["id"] == "sess_abc"
        assert d["modelName"] == "deepseek-v4"
        assert len(d["steps"]) == 1
        assert d["steps"][0]["numeral"] == "壹"
        assert d["steps"][0]["tools"][0]["name"] == "check_format"
        assert d["steps"][0]["tools"][0]["detail"]["data"]["errors"] == 2
        tool_data = d["steps"][0]["tools"][0]
        assert tool_data["callKind"] == "tool"
        assert tool_data["callScope"] == "parent"
        assert tool_data["subagentName"] is None
        assert d["steps"][0]["verdict"] == "格式有问题"
        assert len(d["thoughts"]) == 1
        assert d["thoughts"][0]["text"] == "thinking..."
        assert d["stats"]["tokensIn"] == 100
        assert d["stats"]["tokensOut"] == 50
        assert d["stats"]["elapsed"] == 10.0

    def test_to_detail_dict_includes_new_step_fields(self):
        """Steps serialized after the subagent schema update include metadata."""
        tool = ToolInfo(
            name="check_format",
            skill="格式检查",
            status="done",
            duration=0.5,
            summary="ok",
            handle_id="hdl_abc",
            parent_handle_id="hdl_parent",
            parent_subagent_name="parent_agent",
        )
        step = StepRecord(
            index=3,
            label="check_format",
            skill="格式检查",
            tools=[tool],
            turn_index=2,
            start_segment_index=10,
            end_segment_index=15,
        )
        s = SessionRecord(
            id="sess_abc",
            task="audit",
            file_id="/tmp/test.docx",
            status="completed",
            created_at=1748600000.0,
        )
        s.steps.append(step)

        d = s.to_detail_dict()
        sd = d["steps"][0]
        assert sd["turnIndex"] == 2
        assert sd["startSegmentIndex"] == 10
        assert sd["endSegmentIndex"] == 15
        assert sd["subagents"] == []
        td = sd["tools"][0]
        assert td["handleId"] == "hdl_abc"
        assert td["parentHandleId"] == "hdl_parent"
        assert td["parentSubagentName"] == "parent_agent"

    def test_to_detail_dict_includes_subagents(self):
        """Nested sub-agent trees are serialized in step dicts."""
        from courtier.agent.api.models import (
            SubagentRunRecord,
            SubagentThoughtRecord,
            SubagentToolRecord,
        )

        child_tool = SubagentToolRecord(
            name="search_doc",
            skill="search",
            status="done",
            duration=0.3,
            summary="found",
        )
        child = SubagentRunRecord(
            name="searcher",
            handle_id="hdl_child",
            parent_handle_id="hdl_root",
            status="completed",
            conclusion="搜索完成",
            tools=[child_tool],
        )
        root = SubagentRunRecord(
            name="format_auditor",
            handle_id="hdl_root",
            status="completed",
            conclusion="格式审核通过",
            thoughts=[
                SubagentThoughtRecord(id=1, text="开始检查格式..."),
                SubagentThoughtRecord(id=2, text="检查完毕"),
            ],
            children=[child],
        )
        step = StepRecord(
            index=1,
            label="run_format_auditor",
            skill="format_auditor",
            subagents=[root],
        )
        s = SessionRecord(
            id="sess_abc",
            task="audit",
            file_id="/tmp/test.docx",
            created_at=1748600000.0,
        )
        s.steps.append(step)

        d = s.to_detail_dict()
        sd = d["steps"][0]
        assert len(sd["subagents"]) == 1
        sa = sd["subagents"][0]
        assert sa["name"] == "format_auditor"
        assert sa["handleId"] == "hdl_root"
        assert sa["status"] == "completed"
        assert sa["conclusion"] == "格式审核通过"
        assert len(sa["thoughts"]) == 2
        assert sa["thoughts"][0]["text"] == "开始检查格式..."
        assert len(sa["children"]) == 1
        assert sa["children"][0]["name"] == "searcher"
        assert len(sa["children"][0]["tools"]) == 1

    def test_to_detail_dict_includes_thought_step_and_turn_index(self):
        """Thoughts include stepIndex and turnIndex for precise routing."""
        t = ThoughtRecord(
            id=1,
            text="thinking...",
            turn=1,
            timestamp=123.0,
            step_index=3,
            turn_index=2,
        )
        s = SessionRecord(
            id="sess_abc",
            task="audit",
            file_id="/tmp/test.docx",
            created_at=1748600000.0,
        )
        s.thoughts.append(t)
        d = s.to_detail_dict()
        assert d["thoughts"][0]["stepIndex"] == 3
        assert d["thoughts"][0]["turnIndex"] == 2


class TestTurnConclusions:
    def test_build_turns_uses_per_turn_conclusions(self):
        s = SessionRecord(
            id="sess_abc",
            task="audit",
            file_id="/tmp/f.docx",
            status="completed",
            created_at=100.0,
            turn_messages=[
                {"text": "turn 1", "timestamp": 100.0},
                {"text": "turn 2", "timestamp": 200.0},
            ],
            turn_step_starts=[0, 1],
            turn_conclusions=["结论一", "结论二"],
        )
        s.steps.append(StepRecord(index=1, label="step1", skill="s1"))
        s.steps.append(StepRecord(index=2, label="step2", skill="s2"))

        detail = s.to_detail_dict()
        turns = detail["turns"]
        assert len(turns) == 2
        assert turns[0]["conclusion"] == "结论一"
        assert turns[1]["conclusion"] == "结论二"

    def test_build_turns_falls_back_to_session_conclusion_for_last_turn(self):
        """Legacy sessions without turn_conclusions still show the final conclusion."""
        s = SessionRecord(
            id="sess_abc",
            task="audit",
            file_id="/tmp/f.docx",
            status="completed",
            created_at=100.0,
            conclusion="最终结论",
            turn_messages=[
                {"text": "turn 1", "timestamp": 100.0},
                {"text": "turn 2", "timestamp": 200.0},
            ],
            turn_step_starts=[0, 1],
            turn_conclusions=[],
        )
        s.steps.append(StepRecord(index=1, label="step1", skill="s1"))
        s.steps.append(StepRecord(index=2, label="step2", skill="s2"))

        detail = s.to_detail_dict()
        turns = detail["turns"]
        assert turns[0]["conclusion"] == ""
        assert turns[1]["conclusion"] == "最终结论"


class TestSubagentRunRecord:
    """Round-trip tests for the new sub-agent persistence model."""

    def test_round_trip_minimal(self):
        from courtier.agent.api.models import SubagentRunRecord as SRR

        original = SRR(name="auditor", handle_id="hdl_1", status="completed")
        data = original.to_dict()
        restored = SRR.from_dict(data)
        assert restored.name == "auditor"
        assert restored.handle_id == "hdl_1"
        assert restored.status == "completed"

    def test_round_trip_nested(self):
        from courtier.agent.api.models import (
            SubagentRunRecord as SRR,
            SubagentThoughtRecord,
            SubagentToolRecord,
        )

        original = SRR(
            name="root",
            handle_id="hdl_root",
            status="completed",
            conclusion="done",
            thoughts=[
                SubagentThoughtRecord(id=1, text="thinking..."),
            ],
            tools=[
                SubagentToolRecord(
                    name="parse", status="done", duration=0.5, summary="ok"
                ),
            ],
            children=[
                SRR(
                    name="child",
                    handle_id="hdl_child",
                    parent_handle_id="hdl_root",
                    status="completed",
                ),
            ],
        )
        data = original.to_dict()
        restored = SRR.from_dict(data)
        assert restored.name == "root"
        assert restored.conclusion == "done"
        assert len(restored.thoughts) == 1
        assert restored.thoughts[0].text == "thinking..."
        assert len(restored.tools) == 1
        assert restored.tools[0].name == "parse"
        assert len(restored.children) == 1
        assert restored.children[0].name == "child"
        assert restored.children[0].parent_handle_id == "hdl_root"

    def test_legacy_format_handles_camel_and_snake_case(self):
        """from_dict accepts both camelCase (new) and snake_case (legacy) keys."""
        from courtier.agent.api.models import SubagentRunRecord as SRR

        data = {
            "name": "test",
            "handle_id": "hdl_1",
            "parent_handle_id": "hdl_parent",
            "status": "completed",
        }
        restored = SRR.from_dict(data)
        assert restored.handle_id == "hdl_1"
        assert restored.parent_handle_id == "hdl_parent"

        camel_data = {
            "name": "test",
            "handleId": "hdl_2",
            "parentHandleId": "hdl_parent_2",
            "status": "completed",
        }
        restored2 = SRR.from_dict(camel_data)
        assert restored2.handle_id == "hdl_2"
        assert restored2.parent_handle_id == "hdl_parent_2"
