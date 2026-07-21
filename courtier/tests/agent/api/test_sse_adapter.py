"""Tests for SSEAdapter."""

import asyncio
import json
import tempfile

import pytest

from courtier.agent.api.session_store import SessionStore
from courtier.agent.api.sse_adapter import SSEAdapter
from courtier.agent.core.execution_result import ExecutionResult
from courtier.agent.tools.protocol import ToolResult


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
        adapter = SSEAdapter(
            asyncio.Queue(), session_store=None, session_id="s1", tool_registry=registry
        )

        meta = adapter._tool_meta_for("audit_format")
        assert meta["skill"] == "format_audit"
        assert meta["display_name"] == "格式审核"

    def test_returns_empty_meta_when_tool_not_in_registry(self):
        from courtier.agent.tools.registry import ToolRegistry

        registry = ToolRegistry()
        adapter = SSEAdapter(
            asyncio.Queue(), session_store=None, session_id="s1", tool_registry=registry
        )

        meta = adapter._tool_meta_for("missing_tool")
        assert meta == {"skill": "", "display_name": None, "skill_description": ""}

    def test_returns_empty_meta_without_registry(self):
        adapter = SSEAdapter(asyncio.Queue(), session_store=None, session_id="s1")
        assert adapter._tool_meta_for("any") == {
            "skill": "", "display_name": None, "skill_description": ""
        }


class TestSSEAdapter:
    @pytest.mark.asyncio
    async def test_on_step_think_with_tool_calls(self, store):
        await store.create("sess_7e57e57e57e5", "task", "file_test1234")
        q: asyncio.Queue = asyncio.Queue()
        adapter = SSEAdapter(q, store, "sess_7e57e57e57e5")

        await adapter.on_step("think", "tool_calls: check_format, check_grammar")

        # Check SSE event was emitted
        item = q.get_nowait()
        assert item[0] == "event"
        data_line = item[1]
        assert "data:" in data_line
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
        q: asyncio.Queue = asyncio.Queue()
        adapter = SSEAdapter(q, store, "sess_7e57e57e57e5")

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
        q: asyncio.Queue = asyncio.Queue()
        adapter = SSEAdapter(q, store, "sess_7e57e57e57e5")

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
        q: asyncio.Queue = asyncio.Queue()
        adapter = SSEAdapter(q, store, "sess_7e57e57e57e5")

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
        assert session.steps[0].tools == []
        assert session.steps[1].label == ""
        assert session.thoughts[0].step_index == session.steps[1].index

    @pytest.mark.asyncio
    async def test_text_response_placeholder_is_reused(self, store):
        await store.create("sess_7e57e57e57e5", "task", "file_test1234")
        q: asyncio.Queue = asyncio.Queue()
        adapter = SSEAdapter(q, store, "sess_7e57e57e57e5")

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
        q: asyncio.Queue = asyncio.Queue()
        adapter = SSEAdapter(q, store, "sess_7e57e57e57e5")

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
    async def test_on_step_observe(self, store):
        await store.create("sess_7e57e57e57e5", "task", "file_test1234")
        q: asyncio.Queue = asyncio.Queue()
        adapter = SSEAdapter(q, store, "sess_7e57e57e57e5")

        # First create a step with think
        await adapter.on_step("think", "tool_calls: check_format")
        q.get_nowait()  # consume think event

        # Then observe
        await adapter.on_step("observe", "results_collected")
        item = q.get_nowait()
        parsed = json.loads(item[1].replace("data: ", "").strip())
        assert parsed["type"] == "observe"

    @pytest.mark.asyncio
    async def test_on_token(self, store):
        await store.create("sess_7e57e57e57e5", "task", "file_test1234")
        q: asyncio.Queue = asyncio.Queue()
        adapter = SSEAdapter(q, store, "sess_7e57e57e57e5")

        await adapter.on_token("正在分析文档...")

        item = q.get_nowait()
        parsed = json.loads(item[1].replace("data: ", "").strip())
        assert parsed["type"] == "token"
        assert parsed["text"] == "正在分析文档..."

    @pytest.mark.asyncio
    async def test_on_tool_result_success(self, store):
        await store.create("sess_7e57e57e57e5", "task", "file_test1234")
        # Create a step via think event first
        q: asyncio.Queue = asyncio.Queue()
        adapter = SSEAdapter(q, store, "sess_7e57e57e57e5")

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
        q: asyncio.Queue = asyncio.Queue()
        adapter = SSEAdapter(q, store, "sess_7e57e57e57e5")

        await adapter.on_tool_result("check_format", None, "失败: 文件不存在")

        item = q.get_nowait()
        parsed = json.loads(item[1].replace("data: ", "").strip())
        assert parsed["type"] == "tool_result"
        assert "skill" in parsed

    @pytest.mark.asyncio
    async def test_on_tool_result_emits_default_parent_tool_classification(self, store):
        await store.create("sess_7e57e57e57e5", "task", "file_test1234")
        q: asyncio.Queue = asyncio.Queue()
        adapter = SSEAdapter(q, store, "sess_7e57e57e57e5")

        await adapter.on_step("think", "tool_calls: parse_document")
        q.get_nowait()
        await adapter.on_tool_result(
            "parse_document", ToolResult(success=True), "parsed"
        )

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
        q: asyncio.Queue = asyncio.Queue()
        adapter = SSEAdapter(q, store, "sess_7e57e57e57e5")

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
        q: asyncio.Queue = asyncio.Queue()
        adapter = SSEAdapter(q, store, "sess_7e57e57e57e5")

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
        tool = session.steps[0].tools[0]
        assert tool.call_kind == "tool"
        assert tool.call_scope == "subagent"
        assert tool.subagent_name == "format_auditor"

    @pytest.mark.asyncio
    async def test_on_tool_result_normalizes_invalid_classification_metadata(
        self, store
    ):
        await store.create("sess_7e57e57e57e5", "task", "file_test1234")
        q: asyncio.Queue = asyncio.Queue()
        adapter = SSEAdapter(q, store, "sess_7e57e57e57e5")

        await adapter.on_step("think", "tool_calls: parse_document")
        q.get_nowait()
        result = ToolResult(
            success=True,
            metadata={
                "call_kind": "bad",
                "call_scope": "bad",
                "subagent_name": 123,
            },
        )
        await adapter.on_tool_result("parse_document", result, "parsed")

        parsed = json.loads(q.get_nowait()[1].replace("data: ", "").strip())
        assert parsed["callKind"] == "tool"
        assert parsed["callScope"] == "parent"
        assert parsed["subagentName"] is None

    @pytest.mark.asyncio
    async def test_on_step_usage(self, store):
        await store.create("sess_7e57e57e57e5", "task", "file_test1234")
        q: asyncio.Queue = asyncio.Queue()
        adapter = SSEAdapter(q, store, "sess_7e57e57e57e5")

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
        q: asyncio.Queue = asyncio.Queue()
        pause = asyncio.Event()
        pause.set()  # start paused
        adapter = SSEAdapter(q, store, "sess_7e57e57e57e5", pause_event=pause)

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
        d = SSEAdapter._build_detail_data(result)
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
        d = SSEAdapter._build_detail_data(result)
        assert d is not None
        assert d["type"] == "structured"
        assert d["data"]["key1"] == "value1"
        assert d["data"]["key2"] == 123

    @pytest.mark.asyncio
    async def test_build_detail_data_none_data(self):
        result = ExecutionResult(success=True, actor_type="tool", actor_name="t")
        d = SSEAdapter._build_detail_data(result)
        assert d is None

    @pytest.mark.asyncio
    async def test_build_detail_data_failed(self):
        result = ExecutionResult.from_error(
            actor_type="tool", actor_name="t", error="failure"
        )
        d = SSEAdapter._build_detail_data(result)
        assert d is None

    @pytest.mark.asyncio
    async def test_on_tool_result_emits_display_name_from_registry(self, store):
        await store.create("sess_7e57e57e57e5", "task", "file_test1234")
        q: asyncio.Queue = asyncio.Queue()
        from courtier.agent.tools.registry import ToolRegistry

        registry = ToolRegistry()

        adapter = SSEAdapter(q, store, "sess_7e57e57e57e5", tool_registry=registry)

        await adapter.on_step("think", "tool_calls: parse_document")
        q.get_nowait()
        await adapter.on_tool_result(
            "parse_document", ToolResult(success=True), "parsed"
        )

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
        q: asyncio.Queue = asyncio.Queue()
        from courtier.agent.tools.registry import ToolRegistry

        # Register a mock tool with display_name
        class _MockTool:
            name: str = "parse_document"
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

        adapter = SSEAdapter(q, store, "sess_7e57e57e57e5", tool_registry=registry)

        await adapter.on_step("think", "tool_calls: parse_document")
        q.get_nowait()
        await adapter.on_tool_result(
            "parse_document", ToolResult(success=True), "parsed"
        )

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
        q: asyncio.Queue = asyncio.Queue()
        adapter = SSEAdapter(q, store, "sess_7e57e57e57e5")

        await adapter.on_step("think", "tool_calls: parse_document")
        q.get_nowait()
        await adapter.on_tool_result(
            "parse_document", ToolResult(success=True), "parsed"
        )

        parsed = json.loads(q.get_nowait()[1].replace("data: ", "").strip())
        assert parsed["type"] == "tool_result"
        assert parsed["status"] == "ok"
        assert "duration" in parsed
        assert isinstance(parsed["duration"], (int, float))

    @pytest.mark.asyncio
    async def test_on_tool_result_emits_error_status(self, store):
        """SSE event has status=error when the ExecutionResult is a failure."""
        await store.create("sess_7e57e57e57e5", "task", "file_test1234")
        q: asyncio.Queue = asyncio.Queue()
        adapter = SSEAdapter(q, store, "sess_7e57e57e57e5")

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
        q: asyncio.Queue = asyncio.Queue()
        adapter = SSEAdapter(q, store, "sess_7e57e57e57e5")

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
        q: asyncio.Queue = asyncio.Queue()
        adapter = SSEAdapter(q, store, "sess_7e57e57e57e5")

        await adapter.on_step("think", "tool_calls: parse_document")
        q.get_nowait()
        await adapter.on_tool_result(
            "parse_document",
            ExecutionResult(success=True, actor_type="tool", actor_name="parse_document"),
            "完成 (无返回数据)",
        )

        parsed = json.loads(q.get_nowait()[1].replace("data: ", "").strip())
        assert parsed["type"] == "tool_result"
        assert "detail_data" not in parsed


class TestSSEAdapterSubAgentEvents:
    """Test SSEAdapter.on_subagent_event maps to correct SSE event types."""

    @pytest.mark.asyncio
    async def test_start_event_emits_subagent_start_sse(self, store):
        from courtier.agent.agents.subagent.events import SubAgentStreamEvent

        queue = asyncio.Queue()
        adapter = SSEAdapter(queue, store, "sess_511111111111")
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

        queue = asyncio.Queue()
        adapter = SSEAdapter(queue, store, "sess_511111111111")
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

        queue = asyncio.Queue()
        adapter = SSEAdapter(queue, store, "sess_511111111111")
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

        queue = asyncio.Queue()
        adapter = SSEAdapter(queue, store, "sess_511111111111")
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

        queue = asyncio.Queue()
        adapter = SSEAdapter(queue, store, "sess_511111111111")
        await store.create("sess_511111111111", "task", "file_test1234")

        await adapter.on_subagent_event(
            SubAgentStreamEvent(
                kind="conclusion", subagent_name="parser", text="解析完成"
            )
        )

        tag, line = queue.get_nowait()
        data = json.loads(line.strip().removeprefix("data: ").rstrip("\n"))
        assert data["type"] == "subagent_conclusion"
        assert data["name"] == "parser"
        assert data["text"] == "解析完成"

    @pytest.mark.asyncio
    async def test_end_event_emits_subagent_end_sse(self, store):
        from courtier.agent.agents.subagent.events import SubAgentStreamEvent

        queue = asyncio.Queue()
        adapter = SSEAdapter(queue, store, "sess_511111111111")
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

        queue = asyncio.Queue()
        adapter = SSEAdapter(queue, store, "sess_511111111111")
        await store.create("sess_511111111111", "task", "file_test1234")

        await adapter.on_subagent_event(
            SubAgentStreamEvent(
                kind="conclusion", subagent_name="parser", text=""
            )
        )

        assert queue.empty()


class TestSSEAdapterSubAgentStateAccumulation:
    """Test that on_subagent_event accumulates state for historical persistence."""

    @pytest.mark.asyncio
    async def test_full_lifecycle_builds_correct_tree(self, store):
        from courtier.agent.agents.subagent.events import SubAgentStreamEvent

        queue = asyncio.Queue()
        adapter = SSEAdapter(queue, store, "sess_full01")
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
    async def test_nested_subagents_build_tree(self, store):
        from courtier.agent.agents.subagent.events import SubAgentStreamEvent

        queue = asyncio.Queue()
        adapter = SSEAdapter(queue, store, "sess_nest01")
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

        queue = asyncio.Queue()
        adapter = SSEAdapter(queue, store, "sess_rst01")
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
