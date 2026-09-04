"""Tests for RunRecorder."""

import asyncio
import json
import tempfile
from types import SimpleNamespace

import pytest

from courtier.agent.api.services.run_event_log import RunEventLog
from courtier.agent.api.session_store import SessionStore
from courtier.agent.api.sse_adapter import RunRecorder
from courtier.agent.core.execution_result import ExecutionResult
from courtier.agent.tools.protocol import ToolResult


class _LogQueue:
    """Queue-like facade over a RunEventLog for legacy assertion style."""

    def __init__(self, log):
        self.log = log
        self._cursor = -1  # last seq consumed

    def get_nowait(self):
        entries = self.log.replay_after(self._cursor)
        assert entries, "expected another SSE event"
        self._cursor = entries[0].seq
        return ("event", entries[0].line.split("data: ", 1)[1])

    def empty(self):
        return not self.log.replay_after(self._cursor)


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as d:
        yield SessionStore(d)


class TestToolMetaFor:
    def test_resolves_skill_and_display_name_from_registry(self):
        from courtier.agent.tools.registry import ToolRegistry

        class _MockTool:
            name: str = "audit_format"
            description: str = "audits format"
            display_name: str | None = "格式审核"
            skill: str = "format_audit"
            parameters: dict = {"type": "object", "properties": {}}
            output_schema: dict | None = None
            skip_persist: bool = False
            output_content_type: str | None = None
            input_contract = None
            output_contract = None
            runtime_policy = None

            async def execute(self, **kwargs):
                return ToolResult(success=True)

        registry = ToolRegistry()
        registry.register(_MockTool())
        adapter = RunRecorder(
            asyncio.Queue(), session_store=None, session_id="s1", tool_registry=registry
        )

        meta = adapter._tool_meta_for("audit_format")
        assert meta["skill"] == "format_audit"
        assert meta["display_name"] == "格式审核"

    def test_returns_empty_meta_when_tool_not_in_registry(self):
        from courtier.agent.tools.registry import ToolRegistry

        registry = ToolRegistry()
        adapter = RunRecorder(
            asyncio.Queue(), session_store=None, session_id="s1", tool_registry=registry
        )

        meta = adapter._tool_meta_for("missing_tool")
        assert meta == {"skill": "", "display_name": None, "skill_description": ""}

    def test_returns_empty_meta_without_registry(self):
        adapter = RunRecorder(RunEventLog(), session_store=None, session_id="s1")
        assert adapter._tool_meta_for("any") == {
            "skill": "",
            "display_name": None,
            "skill_description": "",
        }


class TestRunRecorder:
    @pytest.mark.asyncio
    async def test_on_step_think_with_tool_calls(self, store):
        await store.create("sess_7e57e57e57e5", "task", "file_test1234")
        log_q = RunEventLog()
        q = _LogQueue(log_q)
        adapter = RunRecorder(log_q, store, "sess_7e57e57e57e5")

        await adapter.on_step("think", "tool_calls: check_format, check_grammar")

        # Check SSE event was emitted
        item = q.get_nowait()
        assert item[0] == "event"
        data_line = item[1]
        parsed = json.loads(data_line.replace("data: ", "").strip())
        assert parsed["type"] == "think"
        assert "check_format" in parsed["detail"]
        assert "check_grammar" in parsed["detail"]

        # Check session was updated
        session = await store.get("sess_7e57e57e57e5")
        assert session is not None
        assert len(session.steps) == 1
        assert session.steps[0].numeral == "壹"

    @pytest.mark.asyncio
    async def test_on_step_think_text_response(self, store):
        await store.create("sess_7e57e57e57e5", "task", "file_test1234")
        log_q = RunEventLog()
        q = _LogQueue(log_q)
        adapter = RunRecorder(log_q, store, "sess_7e57e57e57e5")

        await adapter.on_step("think", "text_response")

        item = q.get_nowait()
        parsed = json.loads(item[1].replace("data: ", "").strip())
        assert parsed["type"] == "think"
        assert parsed["detail"] == "text_response"

        # A placeholder step should be created so subsequent tokens are routed
        # to their own step (matching the frontend runtime).
        session = await store.get("sess_7e57e57e57e5")
        assert session is not None
        assert len(session.steps) == 1
        assert session.steps[0].label == ""
        assert session.steps[0].tools == []

    @pytest.mark.asyncio
    async def test_text_response_after_tool_calls_creates_placeholder_step(self, store):
        await store.create("sess_7e57e57e57e5", "task", "file_test1234")
        log_q = RunEventLog()
        q = _LogQueue(log_q)
        adapter = RunRecorder(log_q, store, "sess_7e57e57e57e5")

        await adapter.on_step("think", "tool_calls: check_format")
        q.get_nowait()  # consume think event

        await adapter.on_tool_result("check_format", None, "ok")
        q.get_nowait()  # consume tool_result event

        await adapter.on_step("think", "text_response")
        q.get_nowait()  # consume think event

        await adapter.on_token("审核已完成，直接输出最终结论。")
        q.get_nowait()  # consume token event

        session = await store.get("sess_7e57e57e57e5")
        assert session is not None
        assert len(session.steps) == 2
        assert session.steps[0].label == "check_format"
        assert session.steps[0].tools[0].name == "check_format"
        assert session.steps[1].label == ""
        assert len(session.thoughts) == 1
        assert session.thoughts[0].text == "审核已完成，直接输出最终结论。"
        assert session.thoughts[0].step_index == session.steps[1].index

    @pytest.mark.asyncio
    async def test_text_response_before_first_tool_result_creates_placeholder_step(self, store):
        await store.create("sess_7e57e57e57e5", "task", "file_test1234")
        log_q = RunEventLog()
        q = _LogQueue(log_q)
        adapter = RunRecorder(log_q, store, "sess_7e57e57e57e5")

        await adapter.on_step("think", "tool_calls: check_format")
        q.get_nowait()

        await adapter.on_step("think", "text_response")
        q.get_nowait()

        await adapter.on_token("先说明思路")
        q.get_nowait()

        session = await store.get("sess_7e57e57e57e5")
        assert session is not None
        assert len(session.steps) == 2
        assert session.steps[0].label == "check_format"
        # The announced call persists as a pending card until its result
        # settles it — the attach snapshot needs it to mirror live state.
        pending = session.steps[0].tools[0]
        assert pending.name == "check_format"
        assert pending.status == "pending"
        assert session.steps[1].label == ""
        assert session.thoughts[0].step_index == session.steps[1].index

    @pytest.mark.asyncio
    async def test_text_response_placeholder_is_reused(self, store):
        await store.create("sess_7e57e57e57e5", "task", "file_test1234")
        log_q = RunEventLog()
        q = _LogQueue(log_q)
        adapter = RunRecorder(log_q, store, "sess_7e57e57e57e5")

        await adapter.on_step("think", "text_response")
        q.get_nowait()

        await adapter.on_step("think", "text_response")
        q.get_nowait()

        await adapter.on_token("第一段")
        q.get_nowait()
        await adapter.on_token("第二段")
        q.get_nowait()

        session = await store.get("sess_7e57e57e57e5")
        assert session is not None
        assert len(session.steps) == 1
        assert len(session.thoughts) == 2
        assert all(t.step_index == session.steps[0].index for t in session.thoughts)

    @pytest.mark.asyncio
    async def test_subagent_tree_attached_to_tool_step_not_placeholder(self, store):
        await store.create("sess_7e57e57e57e5", "task", "file_test1234")
        log_q = RunEventLog()
        q = _LogQueue(log_q)
        adapter = RunRecorder(log_q, store, "sess_7e57e57e57e5")

        await adapter.on_step("think", "tool_calls: run_format_auditor")
        q.get_nowait()

        result = ToolResult(
            success=True,
            metadata={
                "call_kind": "subagent_run",
                "call_scope": "parent",
                "subagent_name": "format_auditor",
                "handle_id": "hdl_1",
            },
        )
        await adapter.on_tool_result("run_format_auditor", result, "done")
        q.get_nowait()

        # Sub-agent emits conclusion while the tool step is current.
        from courtier.agent.agents.subagent.events import SubAgentStreamEvent

        await adapter.on_subagent_event(
            SubAgentStreamEvent(
                kind="start",
                subagent_name="format_auditor",
                handle_id="hdl_1",
                task="audit",
            )
        )
        q.get_nowait()
        await adapter.on_subagent_event(
            SubAgentStreamEvent(
                kind="conclusion",
                subagent_name="format_auditor",
                handle_id="hdl_1",
                text="子代理结论",
            )
        )
        q.get_nowait()

        # text_response creates placeholder step after the sub-agent tool.
        await adapter.on_step("think", "text_response")
        q.get_nowait()

        # Observe should finalize the sub-agent tree onto the tool step, not the placeholder.
        await adapter.on_step("observe", "results_collected")
        q.get_nowait()

        session = await store.get("sess_7e57e57e57e5")
        assert session is not None
        assert len(session.steps) == 2
        tool_step, placeholder_step = session.steps
        assert tool_step.label == "run_format_auditor"
        assert placeholder_step.label == ""
        assert len(tool_step.subagents) == 1
        assert tool_step.subagents[0].conclusion == "子代理结论"
        assert placeholder_step.subagents == []

    @pytest.mark.asyncio
    async def test_subagent_records_carry_display_names(self, store):
        """Sub-agent run and sub-tool records persist their Chinese display
        names so restored sessions render them instead of raw names."""
        from courtier.agent.agents.subagent.events import SubAgentStreamEvent
        from courtier.agent.tools.registry import ToolRegistry

        class _MockTool:
            name = "convert_document"
            description = "converts documents"
            display_name = "文档解析"
            skill = ""
            parameters: dict = {"type": "object", "properties": {}}
            output_schema: dict | None = None
            skip_persist = False
            output_content_type: str | None = None
            input_contract = None
            output_contract = None
            runtime_policy = None

            async def execute(self, **kwargs):
                return ToolResult(success=True)

        registry = ToolRegistry()
        registry.register(_MockTool())

        await store.create("sess_7e57e57e57e5", "task", "file_test1234")
        log_q = RunEventLog()
        q = _LogQueue(log_q)
        adapter = RunRecorder(log_q, store, "sess_7e57e57e57e5", tool_registry=registry)

        await adapter.on_step("think", "tool_calls: content_audit")
        q.get_nowait()
        await adapter.on_subagent_event(
            SubAgentStreamEvent(
                kind="start",
                subagent_name="content_audit",
                handle_id="hdl_1",
                task="audit",
                display_name="内容审核与纠错",
            )
        )
        q.get_nowait()
        await adapter.on_subagent_event(
            SubAgentStreamEvent(
                kind="tool_result",
                subagent_name="content_audit",
                handle_id="hdl_1",
                tool_name="convert_document",
                tool_status="done",
                tool_duration=1.2,
                tool_summary="完成",
            )
        )
        q.get_nowait()

        session = await store.get("sess_7e57e57e57e5")
        run = session.steps[0].subagents[0]
        assert run.display_name == "内容审核与纠错"
        assert run.tools[0].display_name == "文档解析"

        # Serialization round-trip (persisted JSON → restored record).
        restored = type(run).from_dict(run.to_dict())
        assert restored.display_name == "内容审核与纠错"
        assert restored.tools[0].display_name == "文档解析"

    @pytest.mark.asyncio
    async def test_on_step_observe(self, store):
        await store.create("sess_7e57e57e57e5", "task", "file_test1234")
        log_q = RunEventLog()
        q = _LogQueue(log_q)
        adapter = RunRecorder(log_q, store, "sess_7e57e57e57e5")

        # First create a step with think
        await adapter.on_step("think", "tool_calls: check_format")
        q.get_nowait()  # consume think event

        # Then observe
        await adapter.on_step("observe", "results_collected")
        item = q.get_nowait()
        parsed = json.loads(item[1].replace("data: ", "").strip())
        assert parsed["type"] == "observe"

    @pytest.mark.asyncio
    async def test_observe_flushes_verdict_as_step_verdict_event(self, store):
        """中间过程文本在 observe 时作为 step_verdict 事件发出（而非混入结论）。"""
        await store.create("sess_7e57e57e57e5", "task", "file_test1234")
        log_q = RunEventLog()
        q = _LogQueue(log_q)
        adapter = RunRecorder(log_q, store, "sess_7e57e57e57e5")

        await adapter.on_step("think", "tool_calls: parse_layout")
        q.get_nowait()  # consume think event
        await adapter.on_content_token("文档已解析，共1页。")
        q.get_nowait()  # consume conclusion_token event

        await adapter.on_step("observe", "results_collected")

        # step_verdict 先于 observe 事件发出
        item = q.get_nowait()
        parsed = json.loads(item[1].replace("data: ", "").strip())
        assert parsed["type"] == "step_verdict"
        assert parsed["stepIndex"] == 1
        assert parsed["text"] == "文档已解析，共1页。"
        item = q.get_nowait()
        parsed = json.loads(item[1].replace("data: ", "").strip())
        assert parsed["type"] == "observe"

        # 持久化语义不变：verdict 落在 step 上
        session = await store.get("sess_7e57e57e57e5")
        assert session is not None
        assert session.steps[0].verdict == "文档已解析，共1页。"

    @pytest.mark.asyncio
    async def test_observe_without_verdict_emits_no_step_verdict(self, store):
        await store.create("sess_7e57e57e57e5", "task", "file_test1234")
        log_q = RunEventLog()
        q = _LogQueue(log_q)
        adapter = RunRecorder(log_q, store, "sess_7e57e57e57e5")

        await adapter.on_step("think", "tool_calls: check_format")
        q.get_nowait()  # consume think event

        await adapter.on_step("observe", "results_collected")
        item = q.get_nowait()
        parsed = json.loads(item[1].replace("data: ", "").strip())
        assert parsed["type"] == "observe"
        assert q.empty()

    @pytest.mark.asyncio
    async def test_on_token(self, store):
        await store.create("sess_7e57e57e57e5", "task", "file_test1234")
        log_q = RunEventLog()
        q = _LogQueue(log_q)
        adapter = RunRecorder(log_q, store, "sess_7e57e57e57e5")

        await adapter.on_token("正在分析文档...")

        item = q.get_nowait()
        parsed = json.loads(item[1].replace("data: ", "").strip())
        assert parsed["type"] == "token"
        assert parsed["text"] == "正在分析文档..."

    @pytest.mark.asyncio
    async def test_on_tool_result_success(self, store):
        await store.create("sess_7e57e57e57e5", "task", "file_test1234")
        # Create a step via think event first
        log_q = RunEventLog()
        q = _LogQueue(log_q)
        adapter = RunRecorder(log_q, store, "sess_7e57e57e57e5")

        await adapter.on_step("think", "tool_calls: check_format")
        q.get_nowait()  # consume think event

        await adapter.on_tool_result("check_format", None, "发现 2 处错误")

        item = q.get_nowait()
        parsed = json.loads(item[1].replace("data: ", "").strip())
        assert parsed["type"] == "tool_result"
        assert parsed["name"] == "check_format"
        assert "skill" in parsed
        assert "发现 2 处错误" in parsed["summary"]

    @pytest.mark.asyncio
    async def test_on_tool_result_failure(self, store):
        await store.create("sess_7e57e57e57e5", "task", "file_test1234")
        log_q = RunEventLog()
        q = _LogQueue(log_q)
        adapter = RunRecorder(log_q, store, "sess_7e57e57e57e5")

        await adapter.on_tool_result("check_format", None, "失败: 文件不存在")

        item = q.get_nowait()
        parsed = json.loads(item[1].replace("data: ", "").strip())
        assert parsed["type"] == "tool_result"
        assert "skill" in parsed

    @pytest.mark.asyncio
    async def test_on_tool_result_emits_default_parent_tool_classification(self, store):
        await store.create("sess_7e57e57e57e5", "task", "file_test1234")
        log_q = RunEventLog()
        q = _LogQueue(log_q)
        adapter = RunRecorder(log_q, store, "sess_7e57e57e57e5")

        await adapter.on_step("think", "tool_calls: parse_layout")
        q.get_nowait()
        await adapter.on_tool_result("parse_layout", ToolResult(success=True), "parsed")

        item = q.get_nowait()
        parsed = json.loads(item[1].replace("data: ", "").strip())
        assert parsed["type"] == "tool_result"
        assert parsed["callKind"] == "tool"
        assert parsed["callScope"] == "parent"
        assert parsed["subagentName"] is None

        session = await store.get("sess_7e57e57e57e5")
        assert session is not None
        tool = session.steps[0].tools[0]
        assert tool.call_kind == "tool"
        assert tool.call_scope == "parent"
        assert tool.subagent_name is None

    @pytest.mark.asyncio
    async def test_on_tool_result_emits_subagent_run_classification(self, store):
        await store.create("sess_7e57e57e57e5", "task", "file_test1234")
        log_q = RunEventLog()
        q = _LogQueue(log_q)
        adapter = RunRecorder(log_q, store, "sess_7e57e57e57e5")

        await adapter.on_step("think", "tool_calls: run_format_auditor")
        q.get_nowait()
        result = ToolResult(
            success=True,
            metadata={
                "call_kind": "subagent_run",
                "call_scope": "parent",
                "subagent_name": "format_auditor",
            },
        )
        await adapter.on_tool_result("run_format_auditor", result, "done")

        parsed = json.loads(q.get_nowait()[1].replace("data: ", "").strip())
        assert parsed["callKind"] == "subagent_run"
        assert parsed["callScope"] == "parent"
        assert parsed["subagentName"] == "format_auditor"

        session = await store.get("sess_7e57e57e57e5")
        assert session is not None
        tool = session.steps[0].tools[0]
        assert tool.call_kind == "subagent_run"
        assert tool.call_scope == "parent"
        assert tool.subagent_name == "format_auditor"

    @pytest.mark.asyncio
    async def test_on_tool_result_emits_subagent_tool_classification(self, store):
        await store.create("sess_7e57e57e57e5", "task", "file_test1234")
        log_q = RunEventLog()
        q = _LogQueue(log_q)
        adapter = RunRecorder(log_q, store, "sess_7e57e57e57e5")

        await adapter.on_step("think", "tool_calls: run_format_auditor")
        q.get_nowait()
        result = ToolResult(
            success=True,
            metadata={
                "call_kind": "tool",
                "call_scope": "subagent",
                "subagent_name": "format_auditor",
            },
        )
        await adapter.on_tool_result("[format_auditor] audit_format", result, "checked")

        parsed = json.loads(q.get_nowait()[1].replace("data: ", "").strip())
        assert parsed["name"] == "[format_auditor] audit_format"
        assert parsed["callKind"] == "tool"
        assert parsed["callScope"] == "subagent"
        assert parsed["subagentName"] == "format_auditor"

        session = await store.get("sess_7e57e57e57e5")
        assert session is not None
        # The child-scope result announces a different name than the pending
        # dispatch card, so it appends instead of settling it.
        tools = session.steps[0].tools
        assert tools[0].name == "run_format_auditor"
        assert tools[0].status == "pending"
        tool = tools[1]
        assert tool.call_kind == "tool"
        assert tool.call_scope == "subagent"
        assert tool.subagent_name == "format_auditor"

    @pytest.mark.asyncio
    async def test_on_tool_result_normalizes_invalid_classification_metadata(self, store):
        await store.create("sess_7e57e57e57e5", "task", "file_test1234")
        log_q = RunEventLog()
        q = _LogQueue(log_q)
        adapter = RunRecorder(log_q, store, "sess_7e57e57e57e5")

        await adapter.on_step("think", "tool_calls: parse_layout")
        q.get_nowait()
        result = ToolResult(
            success=True,
            metadata={
                "call_kind": "bad",
                "call_scope": "bad",
                "subagent_name": 123,
            },
        )
        await adapter.on_tool_result("parse_layout", result, "parsed")

        parsed = json.loads(q.get_nowait()[1].replace("data: ", "").strip())
        assert parsed["callKind"] == "tool"
        assert parsed["callScope"] == "parent"
        assert parsed["subagentName"] is None

    @pytest.mark.asyncio
    async def test_on_step_usage(self, store):
        await store.create("sess_7e57e57e57e5", "task", "file_test1234")
        log_q = RunEventLog()
        q = _LogQueue(log_q)
        adapter = RunRecorder(log_q, store, "sess_7e57e57e57e5")

        await adapter.on_step("usage", "150,80")

        item = q.get_nowait()
        parsed = json.loads(item[1].replace("data: ", "").strip())
        assert parsed["type"] == "usage"
        assert parsed["tokensIn"] == 150
        assert parsed["tokensOut"] == 80

        # Session token counts should be updated
        session = await store.get("sess_7e57e57e57e5")
        assert session is not None
        assert session.tokens_in == 150
        assert session.tokens_out == 80

    @pytest.mark.asyncio
    async def test_pause_resume(self, store):
        await store.create("sess_7e57e57e57e5", "task", "file_test1234")
        log_q = RunEventLog()
        pause = asyncio.Event()
        pause.set()  # start paused
        adapter = RunRecorder(log_q, store, "sess_7e57e57e57e5", pause_event=pause)

        # Start on_step in background (will block on pause)
        task = asyncio.create_task(adapter.on_step("think", "text_response"))

        # Give it time to hit the pause check
        # NOTE: This sleep-based synchronization may be fragile on slow CI runners.
        # A proper event-based wait (e.g. wait until the task has entered the pause
        # block via an internal sentinel) would be more robust.
        await asyncio.sleep(0.1)
        assert not task.done()  # should still be waiting

        # Resume
        pause.clear()
        await asyncio.sleep(0.1)
        assert task.done()  # should have completed

    @pytest.mark.asyncio
    async def test_build_detail_data_string(self):
        result = ExecutionResult(
            success=True, actor_type="tool", actor_name="t", raw_data="# 标题\n\n内容"
        )
        d = RunRecorder._build_detail_data(result)
        assert d is not None
        assert d["type"] == "markdown"
        assert "标题" in d["content"]

    @pytest.mark.asyncio
    async def test_build_detail_data_dict(self):
        result = ExecutionResult(
            success=True,
            actor_type="tool",
            actor_name="t",
            raw_data={"key1": "value1", "key2": 123},
        )
        d = RunRecorder._build_detail_data(result)
        assert d is not None
        assert d["type"] == "structured"
        assert d["data"]["key1"] == "value1"
        assert d["data"]["key2"] == 123

    @pytest.mark.asyncio
    async def test_build_detail_data_none_data(self):
        result = ExecutionResult(success=True, actor_type="tool", actor_name="t")
        d = RunRecorder._build_detail_data(result)
        assert d is None

    @pytest.mark.asyncio
    async def test_build_detail_data_failed(self):
        result = ExecutionResult.from_error(actor_type="tool", actor_name="t", error="failure")
        d = RunRecorder._build_detail_data(result)
        assert d is None

    @pytest.mark.asyncio
    async def test_on_tool_result_emits_display_name_from_registry(self, store):
        await store.create("sess_7e57e57e57e5", "task", "file_test1234")
        log_q = RunEventLog()
        q = _LogQueue(log_q)
        from courtier.agent.tools.registry import ToolRegistry

        registry = ToolRegistry()

        adapter = RunRecorder(log_q, store, "sess_7e57e57e57e5", tool_registry=registry)

        await adapter.on_step("think", "tool_calls: parse_layout")
        q.get_nowait()
        await adapter.on_tool_result("parse_layout", ToolResult(success=True), "parsed")

        parsed = json.loads(q.get_nowait()[1].replace("data: ", "").strip())
        assert parsed["type"] == "tool_result"
        assert "displayName" in parsed
        # Without display_name on the tool, it should be null
        assert parsed["displayName"] is None

        session = await store.get("sess_7e57e57e57e5")
        assert session is not None
        tool = session.steps[0].tools[0]
        assert tool.display_name is None

    @pytest.mark.asyncio
    async def test_on_tool_result_emits_display_name_when_tool_has_one(self, store):
        await store.create("sess_7e57e57e57e5", "task", "file_test1234")
        log_q = RunEventLog()
        q = _LogQueue(log_q)
        from courtier.agent.tools.registry import ToolRegistry

        # Register a mock tool with display_name
        class _MockTool:
            name: str = "parse_layout"
            description: str = "parses documents"
            display_name: str | None = "文档解析"
            parameters: dict = {"type": "object", "properties": {}}
            output_schema: dict | None = None
            skip_persist: bool = False
            output_content_type: str | None = None
            input_contract = None
            output_contract = None
            runtime_policy = None

            async def execute(self, **kwargs):
                return ToolResult(success=True)

        registry = ToolRegistry()
        registry.register(_MockTool())

        adapter = RunRecorder(log_q, store, "sess_7e57e57e57e5", tool_registry=registry)

        await adapter.on_step("think", "tool_calls: parse_layout")
        q.get_nowait()
        await adapter.on_tool_result("parse_layout", ToolResult(success=True), "parsed")

        parsed = json.loads(q.get_nowait()[1].replace("data: ", "").strip())
        assert parsed["type"] == "tool_result"
        assert parsed["displayName"] == "文档解析"

        session = await store.get("sess_7e57e57e57e5")
        assert session is not None
        tool = session.steps[0].tools[0]
        assert tool.display_name == "文档解析"

    @pytest.mark.asyncio
    async def test_on_tool_result_emits_status_and_duration(self, store):
        """SSE event includes status and duration fields."""
        await store.create("sess_7e57e57e57e5", "task", "file_test1234")
        log_q = RunEventLog()
        q = _LogQueue(log_q)
        adapter = RunRecorder(log_q, store, "sess_7e57e57e57e5")

        await adapter.on_step("think", "tool_calls: parse_layout")
        q.get_nowait()
        await adapter.on_tool_result("parse_layout", ToolResult(success=True), "parsed")

        parsed = json.loads(q.get_nowait()[1].replace("data: ", "").strip())
        assert parsed["type"] == "tool_result"
        assert parsed["status"] == "ok"
        assert "duration" in parsed
        assert isinstance(parsed["duration"], (int, float))

    @pytest.mark.asyncio
    async def test_on_tool_result_emits_error_status(self, store):
        """SSE event has status=error when the ExecutionResult is a failure."""
        await store.create("sess_7e57e57e57e5", "task", "file_test1234")
        log_q = RunEventLog()
        q = _LogQueue(log_q)
        adapter = RunRecorder(log_q, store, "sess_7e57e57e57e5")

        await adapter.on_step("think", "tool_calls: check_format")
        q.get_nowait()
        await adapter.on_tool_result(
            "check_format",
            ExecutionResult.from_error(
                actor_type="tool",
                actor_name="check_format",
                error="something went wrong",
            ),
            "失败: something went wrong",
        )

        parsed = json.loads(q.get_nowait()[1].replace("data: ", "").strip())
        assert parsed["type"] == "tool_result"
        assert parsed["status"] == "error"

    @pytest.mark.asyncio
    async def test_on_tool_result_emits_detail_when_data_present(self, store):
        """SSE event includes detail when the ExecutionResult carries raw_data."""
        await store.create("sess_7e57e57e57e5", "task", "file_test1234")
        log_q = RunEventLog()
        q = _LogQueue(log_q)
        adapter = RunRecorder(log_q, store, "sess_7e57e57e57e5")

        await adapter.on_step("think", "tool_calls: audit_format")
        q.get_nowait()
        result = ExecutionResult(
            success=True,
            actor_type="tool",
            actor_name="audit_format",
            raw_data={"errors": [{"msg": "bad margin"}], "total_pages": 3},
        )
        await adapter.on_tool_result("audit_format", result, "完成 (2 个字段)")

        parsed = json.loads(q.get_nowait()[1].replace("data: ", "").strip())
        assert parsed["type"] == "tool_result"
        assert "detail_data" in parsed
        assert parsed["detail_data"]["type"] == "structured"
        assert parsed["detail_data"]["data"]["errors"] == [{"msg": "bad margin"}]
        assert parsed["detail_data"]["data"]["total_pages"] == 3

    @pytest.mark.asyncio
    async def test_on_tool_result_no_detail_when_data_none(self, store):
        """SSE event omits detail when the ExecutionResult has no raw_data."""
        await store.create("sess_7e57e57e57e5", "task", "file_test1234")
        log_q = RunEventLog()
        q = _LogQueue(log_q)
        adapter = RunRecorder(log_q, store, "sess_7e57e57e57e5")

        await adapter.on_step("think", "tool_calls: parse_layout")
        q.get_nowait()
        await adapter.on_tool_result(
            "parse_layout",
            ExecutionResult(success=True, actor_type="tool", actor_name="parse_layout"),
            "完成 (无返回数据)",
        )

        parsed = json.loads(q.get_nowait()[1].replace("data: ", "").strip())
        assert parsed["type"] == "tool_result"
        assert "detail_data" not in parsed


class TestRunRecorderSubAgentEvents:
    """Test RunRecorder.on_subagent_event maps to correct SSE event types."""

    @pytest.mark.asyncio
    async def test_start_event_emits_subagent_start_sse(self, store):
        from courtier.agent.agents.subagent.events import SubAgentStreamEvent

        log_queue = RunEventLog()
        queue = _LogQueue(log_queue)
        adapter = RunRecorder(log_queue, store, "sess_511111111111")
        await store.create("sess_511111111111", "task", "file_test1234")

        await adapter.on_subagent_event(
            SubAgentStreamEvent(kind="start", subagent_name="parser", task="解析文档")
        )

        tag, line = queue.get_nowait()
        data = json.loads(line.strip().removeprefix("data: ").rstrip("\n"))
        assert data["type"] == "subagent_start"
        assert data["name"] == "parser"
        assert data["task"] == "解析文档"

    @pytest.mark.asyncio
    async def test_token_event_emits_subagent_token_sse(self, store):
        from courtier.agent.agents.subagent.events import SubAgentStreamEvent

        log_queue = RunEventLog()
        queue = _LogQueue(log_queue)
        adapter = RunRecorder(log_queue, store, "sess_511111111111")
        await store.create("sess_511111111111", "task", "file_test1234")

        await adapter.on_subagent_event(
            SubAgentStreamEvent(kind="token", subagent_name="parser", text="分析中...")
        )

        tag, line = queue.get_nowait()
        data = json.loads(line.strip().removeprefix("data: ").rstrip("\n"))
        assert data["type"] == "subagent_token"
        assert data["name"] == "parser"
        assert data["text"] == "分析中..."

    @pytest.mark.asyncio
    async def test_think_event_emits_subagent_think_sse(self, store):
        from courtier.agent.agents.subagent.events import SubAgentStreamEvent

        log_queue = RunEventLog()
        queue = _LogQueue(log_queue)
        adapter = RunRecorder(log_queue, store, "sess_511111111111")
        await store.create("sess_511111111111", "task", "file_test1234")

        await adapter.on_subagent_event(
            SubAgentStreamEvent(
                kind="think",
                subagent_name="parser",
                handle_id="h-abc123",
                parent_handle_id="h-parent456",
                text="text_response",
            )
        )

        tag, line = queue.get_nowait()
        data = json.loads(line.strip().removeprefix("data: ").rstrip("\n"))
        assert data["type"] == "subagent_think"
        assert data["name"] == "parser"
        assert data["text"] == "text_response"
        assert data["handleId"] == "h-abc123"
        assert data["parentHandleId"] == "h-parent456"

    @pytest.mark.asyncio
    async def test_tool_result_event_emits_subagent_tool_result_sse(self, store):
        from courtier.agent.agents.subagent.events import SubAgentStreamEvent

        log_queue = RunEventLog()
        queue = _LogQueue(log_queue)
        adapter = RunRecorder(log_queue, store, "sess_511111111111")
        await store.create("sess_511111111111", "task", "file_test1234")

        await adapter.on_subagent_event(
            SubAgentStreamEvent(
                kind="tool_result",
                subagent_name="parser",
                tool_name="parse",
                tool_status="ok",
                tool_duration=1.5,
                tool_summary="done",
            )
        )

        tag, line = queue.get_nowait()
        data = json.loads(line.strip().removeprefix("data: ").rstrip("\n"))
        assert data["type"] == "subagent_tool_result"
        assert data["name"] == "parser"
        assert data["toolName"] == "parse"
        assert data["toolStatus"] == "ok"
        assert data["toolDuration"] == 1.5
        assert data["toolSummary"] == "done"

    @pytest.mark.asyncio
    async def test_conclusion_event_emits_subagent_conclusion_sse(self, store):
        from courtier.agent.agents.subagent.events import SubAgentStreamEvent

        log_queue = RunEventLog()
        queue = _LogQueue(log_queue)
        adapter = RunRecorder(log_queue, store, "sess_511111111111")
        await store.create("sess_511111111111", "task", "file_test1234")

        await adapter.on_subagent_event(
            SubAgentStreamEvent(kind="conclusion", subagent_name="parser", text="解析完成")
        )

        tag, line = queue.get_nowait()
        data = json.loads(line.strip().removeprefix("data: ").rstrip("\n"))
        assert data["type"] == "subagent_conclusion"
        assert data["name"] == "parser"
        assert data["text"] == "解析完成"

    @pytest.mark.asyncio
    async def test_end_event_emits_subagent_end_sse(self, store):
        from courtier.agent.agents.subagent.events import SubAgentStreamEvent

        log_queue = RunEventLog()
        queue = _LogQueue(log_queue)
        adapter = RunRecorder(log_queue, store, "sess_511111111111")
        await store.create("sess_511111111111", "task", "file_test1234")

        await adapter.on_subagent_event(
            SubAgentStreamEvent(
                kind="end",
                subagent_name="parser",
                result={"status": "completed", "content": "done"},
            )
        )

        tag, line = queue.get_nowait()
        data = json.loads(line.strip().removeprefix("data: ").rstrip("\n"))
        assert data["type"] == "subagent_end"
        assert data["name"] == "parser"
        assert data["result"] == {"status": "completed", "content": "done"}

    @pytest.mark.asyncio
    async def test_empty_conclusion_event_is_not_emitted(self, store):
        from courtier.agent.agents.subagent.events import SubAgentStreamEvent

        log_queue = RunEventLog()
        queue = _LogQueue(log_queue)
        adapter = RunRecorder(log_queue, store, "sess_511111111111")
        await store.create("sess_511111111111", "task", "file_test1234")

        await adapter.on_subagent_event(
            SubAgentStreamEvent(kind="conclusion", subagent_name="parser", text="")
        )

        assert queue.empty()


class TestRunRecorderSubAgentStateAccumulation:
    """Test that on_subagent_event accumulates state for historical persistence."""

    @pytest.mark.asyncio
    async def test_full_lifecycle_builds_correct_tree(self, store):
        from courtier.agent.agents.subagent.events import SubAgentStreamEvent

        log_queue = RunEventLog()
        queue = _LogQueue(log_queue)
        adapter = RunRecorder(log_queue, store, "sess_full01")
        await store.create("sess_full01", "audit", "/tmp/f.docx")

        # Simulate a step start so the adapter has a current step.
        await adapter.on_step("think", "tool_calls:run_auditor")
        # Queue drain — we only care about state, not SSE output.
        while not queue.empty():
            queue.get_nowait()

        # Sub-agent lifecycle: start → think → token → tool_result → conclusion → end
        await adapter.on_subagent_event(
            SubAgentStreamEvent(
                kind="start",
                subagent_name="format_auditor",
                handle_id="hdl_1",
                task="审核格式",
            )
        )
        await adapter.on_subagent_event(
            SubAgentStreamEvent(
                kind="think",
                subagent_name="format_auditor",
                handle_id="hdl_1",
                text="text_response",
            )
        )
        await adapter.on_subagent_event(
            SubAgentStreamEvent(
                kind="token",
                subagent_name="format_auditor",
                handle_id="hdl_1",
                text="开始检查...",
            )
        )
        await adapter.on_subagent_event(
            SubAgentStreamEvent(
                kind="tool_result",
                subagent_name="format_auditor",
                handle_id="hdl_1",
                tool_name="check_format",
                tool_status="ok",
                tool_duration=0.5,
                tool_summary="格式正确",
            )
        )
        await adapter.on_subagent_event(
            SubAgentStreamEvent(
                kind="conclusion",
                subagent_name="format_auditor",
                handle_id="hdl_1",
                text="审核通过",
            )
        )
        await adapter.on_subagent_event(
            SubAgentStreamEvent(
                kind="end",
                subagent_name="format_auditor",
                handle_id="hdl_1",
                result={"status": "completed"},
            )
        )

        # Drain SSE queue
        while not queue.empty():
            queue.get_nowait()

        tree = adapter._build_subagent_tree()
        assert len(tree) == 1
        sa = tree[0]
        assert sa.name == "format_auditor"
        assert sa.handle_id == "hdl_1"
        assert sa.status == "completed"
        assert sa.task == "审核格式"
        assert sa.conclusion == "审核通过"
        assert len(sa.thoughts) == 1
        assert sa.thoughts[0].text == "开始检查..."
        assert len(sa.tools) == 1
        assert sa.tools[0].name == "check_format"
        assert sa.tools[0].summary == "格式正确"

    @pytest.mark.asyncio
    async def test_running_tree_is_in_snapshot_before_observe(self, store):
        """Attach invariant I2 for sub-agents: each event's tree state is
        persisted (stamped with that event's seq) before the event is
        appended, so a mid-run snapshot already contains the running
        sub-agent and re-attach replay after the watermark reconstructs
        seamlessly instead of dropping events for an unknown node."""
        from courtier.agent.agents.subagent.events import SubAgentStreamEvent

        log_queue = RunEventLog()
        queue = _LogQueue(log_queue)
        adapter = RunRecorder(log_queue, store, "sess_abc123def456")
        await store.create("sess_abc123def456", "audit", "/tmp/f.docx")

        await adapter.on_step("think", "tool_calls:run_auditor")
        queue.get_nowait()  # think event

        await adapter.on_subagent_event(
            SubAgentStreamEvent(
                kind="start",
                subagent_name="format_auditor",
                handle_id="hdl_1",
                task="审核格式",
            )
        )
        queue.get_nowait()  # subagent_start
        await adapter.on_subagent_event(
            SubAgentStreamEvent(
                kind="token",
                subagent_name="format_auditor",
                handle_id="hdl_1",
                text="运行中思考",
            )
        )
        queue.get_nowait()  # subagent_token

        # Mid-run snapshot: watermark == last emitted event's seq, and the
        # owner step already carries the RUNNING sub-agent with its thought.
        session = await store.get("sess_abc123def456")
        assert session is not None
        assert session.event_seq == queue._cursor
        owner_step = session.steps[0]
        assert len(owner_step.subagents) == 1
        sa = owner_step.subagents[0]
        assert sa.status == "running"
        assert sa.thoughts[0].text == "运行中思考"

        # Nothing precedes the watermark unrecorded: replay after it starts
        # empty until genuinely new events arrive.
        assert log_queue.replay_after(session.event_seq) == []

    @pytest.mark.asyncio
    async def test_subagent_events_before_announcement_buffer_to_dispatch_step(self, store):
        """Regression: a skill can start its sub-agent before the bus listener
        records the think.tool_calls step (direct callback vs bus queue).
        Early events must not anchor the tree to the previous tool step —
        that stale copy rendered as a duplicate sub-agent after re-attach."""
        from queue import Queue

        from courtier.agent.agents.subagent.events import SubAgentStreamEvent

        log_q = RunEventLog()
        q = _LogQueue(log_q)
        adapter = RunRecorder(log_q, store, "sess_abc123def456")
        await store.create("sess_abc123def456", "audit", "/tmp/f.docx")

        # Previous tool step — the leak target.
        await adapter.on_step("think", "tool_calls: convert_document")
        q.get_nowait()
        await adapter.on_tool_result("convert_document", ToolResult(success=True), "ok")
        q.get_nowait()
        await adapter.on_step("observe", "")
        q.get_nowait()

        # RACE: the bus still holds unprocessed events (the dispatch
        # announcement among them) when the sub-agent starts via the direct
        # callback.
        adapter._event_subscription = SimpleNamespace(queue=Queue())
        adapter._event_subscription.queue.put("think.tool_calls")
        await adapter.on_subagent_event(
            SubAgentStreamEvent(
                kind="start", subagent_name="content_audit", handle_id="hdl_2", task="审核"
            )
        )
        await adapter.on_subagent_event(
            SubAgentStreamEvent(
                kind="token", subagent_name="content_audit", handle_id="hdl_2", text="早期思考"
            )
        )
        # Buffered, not recorded: no leak into the convert step, no SSE yet.
        session = await store.get("sess_abc123def456")
        assert session is not None
        assert session.steps[0].subagents == []
        assert q.empty()

        # The listener catches up: records the dispatch step, drains the bus,
        # and flushes the buffered events in arrival order.
        await adapter.on_step("think", "tool_calls: content_audit")
        q.get_nowait()  # think
        adapter._event_subscription = SimpleNamespace(queue=Queue())

        await adapter.on_subagent_event(
            SubAgentStreamEvent(
                kind="end",
                subagent_name="content_audit",
                handle_id="hdl_2",
                result={"status": "completed"},
            )
        )
        q.get_nowait()  # subagent_end (start/token flushed behind the think)

        session = await store.get("sess_abc123def456")
        assert session is not None
        assert session.steps[0].subagents == []  # no stale copy
        dispatch = session.steps[1]
        assert len(dispatch.subagents) == 1
        assert dispatch.subagents[0].status == "completed"
        assert dispatch.subagents[0].thoughts[0].text == "早期思考"

        # Log order: the dispatch think precedes the sub-agent start, so a
        # re-attached client anchors the node to the right step.
        types = [entry.payload["type"] for entry in log_q.replay_after(-1)]
        assert max(i for i, t in enumerate(types) if t == "think") < types.index("subagent_start")

    @pytest.mark.asyncio
    async def test_busy_bus_does_not_stall_post_announce_events(self, store):
        """Once the dispatch announcement is recorded, sub-agent events must
        flow immediately even while the bus queue backs up again (SkillTool
        progress events, loop contention with other runs).  A sticky barrier
        would hold every event until the next think/observe — stalling the
        live stream for the whole sub-agent run."""
        from queue import Queue
        from types import SimpleNamespace

        from courtier.agent.agents.subagent.events import SubAgentStreamEvent

        log_q = RunEventLog()
        q = _LogQueue(log_q)
        adapter = RunRecorder(log_q, store, "sess_abc123def456")
        await store.create("sess_abc123def456", "audit", "/tmp/f.docx")

        # Announcement processed with an idle bus, as usual.
        await adapter.on_step("think", "tool_calls: content_audit")
        q.get_nowait()

        # The bus backs up while the sub-agent streams via the direct callback.
        adapter._event_subscription = SimpleNamespace(queue=Queue())
        adapter._event_subscription.queue.put("tool.progress")

        await adapter.on_subagent_event(
            SubAgentStreamEvent(
                kind="start", subagent_name="content_audit", handle_id="hdl_1", task="审核"
            )
        )
        first = q.get_nowait()[1]
        assert json.loads(first.strip().removeprefix("data: ").rstrip("\n"))["type"] == (
            "subagent_start"
        )

        await adapter.on_subagent_event(
            SubAgentStreamEvent(
                kind="token", subagent_name="content_audit", handle_id="hdl_1", text="流式"
            )
        )
        second = q.get_nowait()[1]
        assert json.loads(second.strip().removeprefix("data: ").rstrip("\n"))["type"] == (
            "subagent_token"
        )

        # And the snapshot keeps up with the stream (watermark stamped).
        session = await store.get("sess_abc123def456")
        assert session is not None
        assert session.steps[0].subagents[0].thoughts[0].text == "流式"

    @pytest.mark.asyncio
    async def test_nested_subagents_build_tree(self, store):
        from courtier.agent.agents.subagent.events import SubAgentStreamEvent

        log_queue = RunEventLog()
        queue = _LogQueue(log_queue)
        adapter = RunRecorder(log_queue, store, "sess_nest01")
        await store.create("sess_nest01", "audit", "/tmp/f.docx")

        await adapter.on_step("think", "tool_calls:run_auditor")
        while not queue.empty():
            queue.get_nowait()

        # Parent sub-agent
        await adapter.on_subagent_event(
            SubAgentStreamEvent(
                kind="start",
                subagent_name="orchestrator",
                handle_id="hdl_root",
                task="编排审计",
            )
        )
        # Child sub-agent
        await adapter.on_subagent_event(
            SubAgentStreamEvent(
                kind="start",
                subagent_name="parser",
                handle_id="hdl_child",
                parent_handle_id="hdl_root",
                task="解析文档",
            )
        )
        await adapter.on_subagent_event(
            SubAgentStreamEvent(
                kind="conclusion",
                subagent_name="parser",
                handle_id="hdl_child",
                text="解析完毕",
            )
        )
        await adapter.on_subagent_event(
            SubAgentStreamEvent(
                kind="end",
                subagent_name="parser",
                handle_id="hdl_child",
            )
        )
        await adapter.on_subagent_event(
            SubAgentStreamEvent(
                kind="conclusion",
                subagent_name="orchestrator",
                handle_id="hdl_root",
                text="编排完成",
            )
        )
        await adapter.on_subagent_event(
            SubAgentStreamEvent(
                kind="end",
                subagent_name="orchestrator",
                handle_id="hdl_root",
            )
        )

        while not queue.empty():
            queue.get_nowait()

        tree = adapter._build_subagent_tree()
        assert len(tree) == 1
        root = tree[0]
        assert root.name == "orchestrator"
        assert len(root.children) == 1
        assert root.children[0].name == "parser"
        assert root.children[0].conclusion == "解析完毕"

    @pytest.mark.asyncio
    async def test_state_resets_on_new_step(self, store):
        from courtier.agent.agents.subagent.events import SubAgentStreamEvent

        log_queue = RunEventLog()
        queue = _LogQueue(log_queue)
        adapter = RunRecorder(log_queue, store, "sess_rst01")
        await store.create("sess_rst01", "audit", "/tmp/f.docx")

        # Step 1
        await adapter.on_step("think", "tool_calls:tool_a")
        while not queue.empty():
            queue.get_nowait()
        await adapter.on_subagent_event(
            SubAgentStreamEvent(
                kind="start",
                subagent_name="agent_a",
                handle_id="hdl_a",
                task="task a",
            )
        )
        assert len(adapter._current_subagents) == 1

        # Observe step 1
        await adapter.on_step("observe", "")
        while not queue.empty():
            queue.get_nowait()

        # Step 2 — subagent state should reset
        await adapter.on_step("think", "tool_calls:tool_b")
        while not queue.empty():
            queue.get_nowait()
        assert len(adapter._current_subagents) == 0
        tree = adapter._build_subagent_tree()
        assert tree == []

        await adapter.on_subagent_event(
            SubAgentStreamEvent(
                kind="start",
                subagent_name="agent_b",
                handle_id="hdl_b",
                task="task b",
            )
        )
        assert len(adapter._current_subagents) == 1

    @pytest.mark.asyncio
    async def test_state_cleared_at_observe_not_next_think(self, store):
        """The accumulation window is cleared at observe time: after the
        step's tree is finalized, the next step starts with a clean dict."""
        from courtier.agent.agents.subagent.events import SubAgentStreamEvent

        log_queue = RunEventLog()
        adapter = RunRecorder(log_queue, store, "sess_0b5e2ve00001")
        await store.create("sess_0b5e2ve00001", "audit", "/tmp/f.docx")

        await adapter.on_step("think", "tool_calls:tool_a")
        await adapter.on_subagent_event(
            SubAgentStreamEvent(
                kind="start",
                subagent_name="agent_a",
                handle_id="hdl_a",
                task="task a",
            )
        )
        assert len(adapter._current_subagents) == 1

        await adapter.on_step("observe", "")
        assert len(adapter._current_subagents) == 0
        assert adapter._subagent_parent_map == {}

    @pytest.mark.asyncio
    async def test_parent_survives_late_think_tool_calls(self, store):
        """Regression for the production race: a sub-agent start delivered via
        the direct callback BEFORE the queued think.tool_calls is processed
        must not be wiped when the think event is handled later.

        Previously the think handler reset the accumulation dict, so a bus
        listener backlogged with token events erased the parent run — the
        persisted tree then contained only orphan children.
        """
        from courtier.agent.agents.subagent.events import SubAgentStreamEvent

        log_queue = RunEventLog()
        adapter = RunRecorder(log_queue, store, "sess_aaaa1111bbbb")
        await store.create("sess_aaaa1111bbbb", "audit", "/tmp/f.docx")

        # Direct callback delivers the parent start first (production ordering
        # when the bus listener is still chewing through think-phase tokens).
        await adapter.on_subagent_event(
            SubAgentStreamEvent(
                kind="start",
                subagent_name="full_government_audit",
                handle_id="hdl_parent",
                task="完整审核",
            )
        )
        # The queued think.tool_calls is processed late.
        await adapter.on_step("think", "tool_calls:full_government_audit")
        assert len(adapter._current_subagents) == 1  # parent NOT wiped

        # Children arrive after, nested under the parent.
        await adapter.on_subagent_event(
            SubAgentStreamEvent(
                kind="start",
                subagent_name="format_audit",
                handle_id="hdl_child",
                parent_handle_id="hdl_parent",
                task="格式审核",
            )
        )
        await adapter.on_subagent_event(
            SubAgentStreamEvent(
                kind="conclusion",
                subagent_name="full_government_audit",
                handle_id="hdl_parent",
                text="父结论",
            )
        )

        # In-memory tree right before observe: parent with nested child.
        tree = adapter._build_subagent_tree()
        assert len(tree) == 1
        assert tree[0].name == "full_government_audit"
        assert [c.name for c in tree[0].children] == ["format_audit"]
        assert tree[0].conclusion == "父结论"

        await adapter.on_step("observe", "")
        assert len(adapter._current_subagents) == 0  # cleared after finalize

        # And the finalized step record carries the nested tree.
        from pathlib import Path

        raw = json.loads((Path(store._dir) / "sess_aaaa1111bbbb.json").read_text(encoding="utf-8"))
        runs = raw["steps"][0]["subagents"]
        assert len(runs) == 1
        assert runs[0]["name"] == "full_government_audit"
        assert [c["name"] for c in runs[0]["children"]] == ["format_audit"]


class TestRunRecorderIssueCounts:
    """issue_counts 从结果 metadata 到 SSE 事件与持久化记录的透传。"""

    @pytest.mark.asyncio
    async def test_on_tool_result_emits_issue_counts(self, store):
        await store.create("sess_7e57e57e57e5", "task", "file_test1234")
        log_q = RunEventLog()
        q = _LogQueue(log_q)
        adapter = RunRecorder(log_q, store, "sess_7e57e57e57e5")

        await adapter.on_step("think", "tool_calls: check_format")
        q.get_nowait()

        counts = {"err": 3, "warn": 0, "ok": 1, "unchecked": 2}
        result = ExecutionResult(
            success=True,
            actor_type="tool",
            actor_name="check_format",
            metadata={"issue_counts": counts},
        )
        await adapter.on_tool_result("check_format", result, "发现 3 处错误")

        parsed = json.loads(q.get_nowait()[1].replace("data: ", "").strip())
        assert parsed["type"] == "tool_result"
        assert parsed["issueCounts"] == counts

        session = await store.get("sess_7e57e57e57e5")
        assert session is not None
        assert session.steps[0].tools[0].issue_counts == counts

    @pytest.mark.asyncio
    async def test_on_tool_result_without_issue_counts_omits_field(self, store):
        await store.create("sess_7e57e57e57e5", "task", "file_test1234")
        log_q = RunEventLog()
        q = _LogQueue(log_q)
        adapter = RunRecorder(log_q, store, "sess_7e57e57e57e5")

        await adapter.on_step("think", "tool_calls: parse_layout")
        q.get_nowait()
        await adapter.on_tool_result("parse_layout", ToolResult(success=True), "parsed")

        parsed = json.loads(q.get_nowait()[1].replace("data: ", "").strip())
        assert "issueCounts" not in parsed

        session = await store.get("sess_7e57e57e57e5")
        assert session is not None
        assert session.steps[0].tools[0].issue_counts is None

    @pytest.mark.asyncio
    async def test_dispatch_tool_result_event_carries_issue_counts(self, store):
        """Event-bus 路径：tool.result payload 的 issue_counts 透传到 SSE。"""
        from courtier.agent.core.events import AgentEvent

        await store.create("sess_7e57e57e57e5", "task", "file_test1234")
        log_q = RunEventLog()
        q = _LogQueue(log_q)
        adapter = RunRecorder(log_q, store, "sess_7e57e57e57e5")

        await adapter.on_step("think", "tool_calls: check_format")
        q.get_nowait()

        counts = {"err": 1, "warn": 2, "ok": 5}
        await adapter._dispatch_event(
            AgentEvent(
                type="tool.result",
                session_id="sess_7e57e57e57e5",
                agent_name="orchestrator",
                turn_index=0,
                payload={
                    "name": "check_format",
                    "summary": "发现 1 处错误",
                    "success": True,
                    "error": None,
                    "issue_counts": counts,
                },
            )
        )

        parsed = json.loads(q.get_nowait()[1].replace("data: ", "").strip())
        assert parsed["type"] == "tool_result"
        assert parsed["issueCounts"] == counts

        session = await store.get("sess_7e57e57e57e5")
        assert session is not None
        assert session.steps[0].tools[0].issue_counts == counts

    @pytest.mark.asyncio
    async def test_dispatch_context_compacted_event_emits_sse(self, store):
        """Event-bus 路径（生产主路径）：context.compacted → SSE context_compacted。"""
        from courtier.agent.core.events import AgentEvent

        await store.create("sess_7e57e57e57e5", "task", "file_test1234")
        log_q = RunEventLog()
        q = _LogQueue(log_q)
        adapter = RunRecorder(log_q, store, "sess_7e57e57e57e5")

        await adapter._dispatch_event(
            AgentEvent(
                type="context.compacted",
                session_id="sess_7e57e57e57e5",
                agent_name="orchestrator",
                turn_index=0,
                payload={"detail": "12 条消息 → 3 条"},
            )
        )

        parsed = json.loads(q.get_nowait()[1].replace("data: ", "").strip())
        assert parsed["type"] == "context_compacted"
        assert parsed["detail"] == "12 条消息 → 3 条"

    @pytest.mark.asyncio
    async def test_dispatch_context_compacting_event_emits_sse(self, store):
        """Event-bus 路径（生产主路径）：context.compacting → SSE context_compacting。"""
        from courtier.agent.core.events import AgentEvent

        await store.create("sess_7e57e57e57e5", "task", "file_test1234")
        log_q = RunEventLog()
        q = _LogQueue(log_q)
        adapter = RunRecorder(log_q, store, "sess_7e57e57e57e5")

        await adapter._dispatch_event(
            AgentEvent(
                type="context.compacting",
                session_id="sess_7e57e57e57e5",
                agent_name="orchestrator",
                turn_index=0,
                payload={"detail": ""},
            )
        )

        parsed = json.loads(q.get_nowait()[1].replace("data: ", "").strip())
        assert parsed["type"] == "context_compacting"

    @pytest.mark.asyncio
    async def test_subagent_tool_result_emits_and_persists_issue_counts(self, store):
        from courtier.agent.agents.subagent.events import SubAgentStreamEvent

        log_queue = RunEventLog()
        queue = _LogQueue(log_queue)
        adapter = RunRecorder(log_queue, store, "sess_c01c01c01c01")
        await store.create("sess_c01c01c01c01", "audit", "/tmp/f.docx")

        await adapter.on_step("think", "tool_calls:run_format_auditor")
        while not queue.empty():
            queue.get_nowait()

        await adapter.on_subagent_event(
            SubAgentStreamEvent(
                kind="start",
                subagent_name="format_auditor",
                handle_id="hdl_1",
                task="审核格式",
            )
        )
        counts = {"err": 2, "warn": 1, "ok": 8, "unchecked": 3}
        await adapter.on_subagent_event(
            SubAgentStreamEvent(
                kind="tool_result",
                subagent_name="format_auditor",
                handle_id="hdl_1",
                tool_name="check_format",
                tool_status="ok",
                tool_duration=0.5,
                tool_summary="格式正确",
                tool_issue_counts=counts,
            )
        )

        # SSE event carries the counts.
        sse_events = []
        while not queue.empty():
            tag, line = queue.get_nowait()
            sse_events.append(json.loads(line.strip().removeprefix("data: ").rstrip("\n")))
        tool_result_events = [e for e in sse_events if e["type"] == "subagent_tool_result"]
        assert len(tool_result_events) == 1
        assert tool_result_events[0]["issueCounts"] == counts

        # In-memory accumulation carries the counts.
        tree = adapter._build_subagent_tree()
        assert tree[0].tools[0].issue_counts == counts

        # After observe the finalized step record persists the counts.
        await adapter.on_step("observe", "")
        session = await store.get("sess_c01c01c01c01")
        assert session is not None
        persisted_tool = session.steps[0].subagents[0].tools[0]
        assert persisted_tool.issue_counts == counts
        detail = session.to_detail_dict()
        assert detail["steps"][0]["subagents"][0]["tools"][0]["issueCounts"] == counts

    @pytest.mark.asyncio
    async def test_subagent_tool_result_without_issue_counts_omits_field(self, store):
        from courtier.agent.agents.subagent.events import SubAgentStreamEvent

        log_queue = RunEventLog()
        queue = _LogQueue(log_queue)
        adapter = RunRecorder(log_queue, store, "sess_c02c02c02c02")
        await store.create("sess_c02c02c02c02", "audit", "/tmp/f.docx")

        await adapter.on_subagent_event(
            SubAgentStreamEvent(
                kind="start",
                subagent_name="parser",
                handle_id="hdl_1",
                task="解析文档",
            )
        )
        await adapter.on_subagent_event(
            SubAgentStreamEvent(
                kind="tool_result",
                subagent_name="parser",
                handle_id="hdl_1",
                tool_name="parse",
                tool_status="ok",
                tool_duration=1.0,
                tool_summary="done",
            )
        )

        sse_events = []
        while not queue.empty():
            tag, line = queue.get_nowait()
            sse_events.append(json.loads(line.strip().removeprefix("data: ").rstrip("\n")))
        tool_result_events = [e for e in sse_events if e["type"] == "subagent_tool_result"]
        assert len(tool_result_events) == 1
        assert "issueCounts" not in tool_result_events[0]
        assert adapter._current_subagents["hdl_1"].tools[0].issue_counts is None


class TestRunRecorderWatermark:
    """reserve → persist(event_seq) → append(seq) 打点顺序与终态事件。"""

    @pytest.mark.asyncio
    async def test_persisted_events_stamp_matching_seq(self, store):
        """每条带持久化的事件,其 store 水位 == 该事件在日志中的 seq。"""
        await store.create("sess_7e57e57e57e5", "task", "file_test1234")
        log = RunEventLog()
        recorder = RunRecorder(log, store, "sess_7e57e57e57e5")

        await recorder.on_step("think", "tool_calls: check_format")
        session = await store.get("sess_7e57e57e57e5")
        think_entries = [e for e in log.replay_after(-1) if e.payload["type"] == "think"]
        assert session.event_seq == think_entries[-1].seq

        await recorder.on_tool_result("check_format", None, "ok")
        session = await store.get("sess_7e57e57e57e5")
        tool_entries = [e for e in log.replay_after(-1) if e.payload["type"] == "tool_result"]
        assert session.event_seq == tool_entries[-1].seq

    @pytest.mark.asyncio
    async def test_log_lines_carry_id_field(self, store):
        await store.create("sess_7e57e57e57e5", "task", "file_test1234")
        log = RunEventLog()
        recorder = RunRecorder(log, store, "sess_7e57e57e57e5")

        await recorder.on_step("think", "text_response")
        entry = log.replay_after(-1)[-1]
        assert entry.line.startswith(f"id: {entry.seq}\ndata: ")

    @pytest.mark.asyncio
    async def test_step_starts_marked_as_eviction_boundaries(self, store):
        await store.create("sess_7e57e57e57e5", "task", "file_test1234")
        log = RunEventLog(max_events=4)
        recorder = RunRecorder(log, store, "sess_7e57e57e57e5")

        await recorder.on_step("think", "tool_calls: check_format")
        await recorder.on_tool_start("check_format")
        await recorder.on_tool_result("check_format", None, "ok")
        await recorder.on_step("observe", "results_collected")
        await recorder.on_step("think", "tool_calls: check_format")
        await recorder.on_tool_start("check_format")
        await recorder.on_tool_result("check_format", None, "ok")
        # Eviction stayed boundary-aligned: no truncation, and every
        # watermark at/after first_seq still replays cleanly.
        assert log.truncated is False

    @pytest.mark.asyncio
    async def test_emit_terminal_seals_log_with_tokens(self, store):
        await store.create("sess_7e57e57e57e5", "task", "file_test1234")
        log = RunEventLog()
        recorder = RunRecorder(log, store, "sess_7e57e57e57e5")

        await recorder.on_step("usage", "150,80")
        line = await recorder.emit_terminal("complete", conclusion="结论")
        assert log.sealed
        terminal = log.replay_after(-1)[-1]
        assert terminal.payload["type"] == "complete"
        assert terminal.payload["conclusion"] == "结论"
        assert terminal.payload["tokensIn"] == 150
        assert terminal.payload["tokensOut"] == 80
        assert line.startswith(f"id: {terminal.seq}\n")

    @pytest.mark.asyncio
    async def test_emit_terminal_error_carries_detail_and_trace(self, store):
        await store.create("sess_7e57e57e57e5", "task", "file_test1234")
        log = RunEventLog()
        recorder = RunRecorder(log, store, "sess_7e57e57e57e5")

        await recorder.emit_terminal("error", detail="服务器内部错误，请稍后重试", trace_id="ab12")
        terminal = log.replay_after(-1)[-1]
        assert terminal.payload["type"] == "error"
        assert terminal.payload["detail"] == "服务器内部错误，请稍后重试"
        assert terminal.payload["trace_id"] == "ab12"


class TestListenerIsolation:
    """A poisoned event must skip that event only, never kill the listener."""

    @pytest.mark.asyncio
    async def test_listener_survives_handler_exception(self, store):
        from courtier.agent.core.event_bus import EventBus
        from courtier.agent.core.events import AgentEvent

        session_id = "sess_iso00000001"
        await store.create(session_id, "task", "file_test1234")
        bus = EventBus()
        log = RunEventLog()
        recorder = RunRecorder(log, store, session_id)
        original_on_token = recorder.on_token

        async def flaky_on_token(text):
            if text == "poison":
                raise RuntimeError("handler boom")
            await original_on_token(text)

        recorder.on_token = flaky_on_token
        recorder.start_listening(bus)
        try:
            for text in ("hello ", "poison", "world"):
                await bus.publish(
                    AgentEvent(
                        type="llm.token",
                        session_id=session_id,
                        agent_name="agent",
                        turn_index=0,
                        payload={"text": text},
                    )
                )
            await recorder.drain_pending(timeout=2.0)
        finally:
            recorder.stop_listening()

        rendered = "".join(e.line for e in log.replay_after(-1))
        assert "hello " in rendered
        assert "world" in rendered
        assert "poison" not in rendered

    def test_render_sse_line_sanitizes_unserializable_payload(self):
        from courtier.agent.api.services.run_event_log import render_sse_line

        class _Opaque:
            def __str__(self):
                return "opaque"

        line = render_sse_line(3, {"type": "observe", "meta": {"obj": _Opaque()}})
        assert '"obj": "opaque"' in line
