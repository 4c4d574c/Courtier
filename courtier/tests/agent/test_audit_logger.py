"""Tests for AuditLogger and its integration with agent_loop."""

from __future__ import annotations

import json

import pytest

from courtier.agent.core.audit_logger import (
    AuditLogger,
    LLMRequestRecord,
    LLMResponseRecord,
    ToolExecutionRecord,
    TurnRecord,
)
from courtier.agent.core.loop import agent_loop
from courtier.agent.core.model import ToolCall
from courtier.agent.core.state import AgentState
from courtier.agent.testing import MockModelClient


class TestAuditLoggerUnit:
    """Unit tests for AuditLogger persistence."""

    def test_for_run_creates_directory(self, tmp_path):
        logger = AuditLogger.for_run(
            agent_name="TestAgent", base_dir=str(tmp_path / "logs")
        )
        assert logger.run_dir.exists()
        assert logger.run_dir.name.startswith("TestAgent_")

    def test_write_turn_persists_files(self, tmp_path):
        logger = AuditLogger.for_run(
            agent_name="TestAgent", base_dir=str(tmp_path / "logs")
        )
        request = LLMRequestRecord(
            messages=[{"role": "user", "content": "hello"}],
            tools=None,
            model="test-model",
            temperature=0.5,
        )
        response = LLMResponseRecord(
            content="Hi there",
            reasoning=None,
            tool_calls=[],
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            finish_reason="stop",
            duration_ms=100,
        )
        turn = TurnRecord(
            turn_index=0,
            timestamp=1234567890.0,
            request=request,
            response=response,
            tool_executions=(),
        )
        logger.write_turn(turn)

        turn_dir = logger.run_dir / "turn_000"
        assert turn_dir.exists()
        assert (turn_dir / "llm_request.json").exists()
        assert (turn_dir / "llm_response.json").exists()
        assert (turn_dir / "tool_executions.json").exists()

        # Verify request content
        req_data = json.loads((turn_dir / "llm_request.json").read_text())
        assert req_data["model"] == "test-model"
        assert req_data["messages"] == [{"role": "user", "content": "hello"}]

        # Verify response content
        resp_data = json.loads((turn_dir / "llm_response.json").read_text())
        assert resp_data["content"] == "Hi there"
        assert resp_data["usage"]["total_tokens"] == 15

        # Verify tool executions (empty list)
        tools_data = json.loads((turn_dir / "tool_executions.json").read_text())
        assert tools_data == []

    def test_write_turn_with_tool_executions(self, tmp_path):
        logger = AuditLogger.for_run(
            agent_name="TestAgent", base_dir=str(tmp_path / "logs")
        )
        request = LLMRequestRecord(
            messages=[{"role": "user", "content": "run tool"}],
            tools=None,
            model="test-model",
            temperature=0.5,
        )
        response = LLMResponseRecord(
            content=None,
            reasoning="I need to run a tool",
            tool_calls=[{"id": "call_1", "name": "echo", "arguments": {"text": "hi"}}],
            usage=None,
            finish_reason="tool_calls",
            duration_ms=200,
        )
        tool_exec = ToolExecutionRecord(
            tool_name="echo",
            tool_call_id="call_1",
            arguments={"text": "hi"},
            result_success=True,
            result_data="hi",
            result_error=None,
            duration_ms=50,
        )
        turn = TurnRecord(
            turn_index=1,
            timestamp=1234567890.0,
            request=request,
            response=response,
            tool_executions=(tool_exec,),
        )
        logger.write_turn(turn)

        turn_dir = logger.run_dir / "turn_001"
        tools_data = json.loads((turn_dir / "tool_executions.json").read_text())
        assert len(tools_data) == 1
        assert tools_data[0]["tool_name"] == "echo"
        assert tools_data[0]["arguments"] == {"text": "hi"}
        assert tools_data[0]["result_success"] is True
        assert tools_data[0]["result_data"] == "hi"

        resp_data = json.loads((turn_dir / "llm_response.json").read_text())
        assert resp_data["reasoning"] == "I need to run a tool"
        assert resp_data["tool_calls"][0]["name"] == "echo"

    def test_finalize_writes_manifest(self, tmp_path):
        logger = AuditLogger.for_run(
            agent_name="TestAgent", base_dir=str(tmp_path / "logs")
        )
        logger.finalize("completed", termination_reason="done")

        manifest_path = logger.run_dir / "run.json"
        assert manifest_path.exists()

        manifest = json.loads(manifest_path.read_text())
        assert manifest["agent_name"] == "TestAgent"
        assert manifest["final_status"] == "completed"
        assert manifest["final_termination_reason"] == "done"
        assert manifest["total_turns"] == 0
        assert manifest["total_usage"]["prompt_tokens"] == 0

    def test_finalize_is_idempotent(self, tmp_path):
        logger = AuditLogger.for_run(
            agent_name="TestAgent", base_dir=str(tmp_path / "logs")
        )
        logger.finalize("completed")
        # Should not raise
        logger.finalize("completed")

    def test_finalize_after_write_turn_aggregates_usage(self, tmp_path):
        logger = AuditLogger.for_run(
            agent_name="TestAgent", base_dir=str(tmp_path / "logs")
        )
        request = LLMRequestRecord(
            messages=[],
            tools=None,
            model="test",
            temperature=None,
        )
        response = LLMResponseRecord(
            content="ok",
            reasoning=None,
            tool_calls=[],
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            finish_reason="stop",
            duration_ms=100,
        )
        turn = TurnRecord(
            turn_index=0,
            timestamp=0.0,
            request=request,
            response=response,
            tool_executions=(),
        )
        logger.write_turn(turn)
        logger.finalize("completed")

        manifest = json.loads((logger.run_dir / "run.json").read_text())
        assert manifest["total_turns"] == 1
        assert manifest["total_usage"]["prompt_tokens"] == 10
        assert manifest["total_usage"]["completion_tokens"] == 5
        assert manifest["total_usage"]["total_tokens"] == 15

    def test_write_turn_after_finalize_raises(self, tmp_path):
        logger = AuditLogger.for_run(
            agent_name="TestAgent", base_dir=str(tmp_path / "logs")
        )
        logger.finalize("completed")

        request = LLMRequestRecord(
            messages=[], tools=None, model="test", temperature=None
        )
        response = LLMResponseRecord(
            content="ok",
            reasoning=None,
            tool_calls=[],
            usage=None,
            finish_reason="stop",
            duration_ms=0,
        )
        turn = TurnRecord(
            turn_index=0,
            timestamp=0.0,
            request=request,
            response=response,
            tool_executions=(),
        )
        with pytest.raises(RuntimeError, match="Cannot write turn after logger is finalized"):
            logger.write_turn(turn)


class TestAuditLoggerIntegration:
    """Integration tests for audit logging in agent_loop."""

    @pytest.mark.asyncio
    async def test_agent_loop_records_turn(self, tmp_path, registry_with_echo):
        """agent_loop with audit_logger writes turn files."""
        logger = AuditLogger.for_run(
            agent_name="TestAgent", base_dir=str(tmp_path / "logs")
        )
        tc = ToolCall(id="call_1", name="echo", arguments={"text": "hello"})
        model = MockModelClient(tool_calls=[tc])

        state = AgentState.initial(task="echo hello")
        final = await agent_loop(
            state=state,
            model=model,
            tool_registry=registry_with_echo,
            audit_logger=logger,
        )

        assert final.status == "completed"

        # Verify turn directory
        turn_dir = logger.run_dir / "turn_000"
        assert turn_dir.exists()
        assert (turn_dir / "llm_request.json").exists()
        assert (turn_dir / "llm_response.json").exists()
        assert (turn_dir / "tool_executions.json").exists()

        # Verify request contains messages (user task + pre-turn reminder)
        req_data = json.loads((turn_dir / "llm_request.json").read_text())
        assert len(req_data["messages"]) == 2
        assert req_data["messages"][0]["role"] == "user"
        assert "echo hello" in req_data["messages"][0]["content"]

        # Verify response contains tool calls
        resp_data = json.loads((turn_dir / "llm_response.json").read_text())
        assert len(resp_data["tool_calls"]) == 1
        assert resp_data["tool_calls"][0]["name"] == "echo"

        # Verify manifest
        logger.finalize(final.status, final.termination_reason)
        manifest = json.loads((logger.run_dir / "run.json").read_text())
        assert manifest["final_status"] == "completed"
        # Two turns: first with tool call, second with text response
        assert manifest["total_turns"] == 2

    @pytest.mark.asyncio
    async def test_agent_loop_records_tool_execution(self, tmp_path, registry_with_echo):
        """Tool execution details are recorded in the turn."""
        logger = AuditLogger.for_run(
            agent_name="TestAgent", base_dir=str(tmp_path / "logs")
        )
        tc = ToolCall(id="call_1", name="echo", arguments={"text": "audit test"})
        model = MockModelClient(tool_calls=[tc])

        state = AgentState.initial(task="echo audit test")
        await agent_loop(
            state=state,
            model=model,
            tool_registry=registry_with_echo,
            audit_logger=logger,
        )

        tools_data = json.loads(
            (logger.run_dir / "turn_000" / "tool_executions.json").read_text()
        )
        assert len(tools_data) == 1
        assert tools_data[0]["tool_name"] == "echo"
        assert tools_data[0]["arguments"] == {"text": "audit test"}
        assert tools_data[0]["result_success"] is True
        assert tools_data[0]["result_data"] == "audit test"
        assert tools_data[0]["result_error"] is None
        assert tools_data[0]["duration_ms"] >= 0

    @pytest.mark.asyncio
    async def test_no_logs_when_audit_logger_none(self, tmp_path, registry_with_echo):
        """When audit_logger is not passed, no log directory is created."""
        tc = ToolCall(id="call_1", name="echo", arguments={"text": "hello"})
        model = MockModelClient(tool_calls=[tc])

        state = AgentState.initial(task="echo hello")
        log_base = tmp_path / "no_logs"

        await agent_loop(
            state=state,
            model=model,
            tool_registry=registry_with_echo,
        )

        assert not log_base.exists()

    @pytest.mark.asyncio
    async def test_agent_loop_finalizes_on_error(self, tmp_path):
        """When model fails, run manifest is written with error status."""
        logger = AuditLogger.for_run(
            agent_name="TestAgent", base_dir=str(tmp_path / "logs")
        )

        class _FailingModel:
            model_name = "failing-model"
            temperature = 0.0

            async def generate(self, messages, tools=None, **kwargs):
                raise RuntimeError("Connection refused")

            async def generate_stream_full(
                self, messages, tools=None, on_token=None, on_content_token=None, **kwargs
            ):
                return await self.generate(messages, tools=tools, **kwargs)

        state = AgentState.initial(task="test")
        final = await agent_loop(
            state=state,
            model=_FailingModel(),
            tool_registry=None,
            audit_logger=logger,
        )

        assert final.status == "error"

        manifest = json.loads((logger.run_dir / "run.json").read_text())
        assert manifest["final_status"] == "error"
        assert "Connection refused" in manifest["final_termination_reason"]

    @pytest.mark.asyncio
    async def test_multi_turn_logging(self, tmp_path, registry_with_echo):
        """Multiple turns create multiple turn directories."""
        logger = AuditLogger.for_run(
            agent_name="TestAgent", base_dir=str(tmp_path / "logs")
        )
        # First model call: tool call, second: text response
        tc = ToolCall(id="1", name="echo", arguments={"text": "ping"})
        model = MockModelClient(tool_calls=[tc])

        state = AgentState.initial(task="echo ping")
        await agent_loop(
            state=state,
            model=model,
            tool_registry=registry_with_echo,
            audit_logger=logger,
        )

        # Should have 2 turns: first with tool call, second with text response
        assert (logger.run_dir / "turn_000").exists()
        assert (logger.run_dir / "turn_001").exists()

        # Turn 0 should have tool executions
        tools_0 = json.loads(
            (logger.run_dir / "turn_000" / "tool_executions.json").read_text()
        )
        assert len(tools_0) == 1

        # Turn 1 should have no tool executions (text response)
        tools_1 = json.loads(
            (logger.run_dir / "turn_001" / "tool_executions.json").read_text()
        )
        assert tools_1 == []

        # Turn 1 response should have content but no tool calls
        resp_1 = json.loads(
            (logger.run_dir / "turn_001" / "llm_response.json").read_text()
        )
        assert resp_1["content"] == "Done."
        assert resp_1["tool_calls"] == []

    @pytest.mark.asyncio
    async def test_permission_blocked_records_turn(self, tmp_path, registry_with_echo):
        """When permission gate blocks a tool, the turn is still recorded."""
        from courtier.agent.permissions.gate import PermissionGate

        logger = AuditLogger.for_run(
            agent_name="TestAgent", base_dir=str(tmp_path / "logs")
        )
        tc = ToolCall(id="call_1", name="echo", arguments={"text": "secret"})
        model = MockModelClient(tool_calls=[tc])

        gate = PermissionGate()
        gate.block("echo")

        state = AgentState.initial(task="echo secret")
        final = await agent_loop(
            state=state,
            model=model,
            tool_registry=registry_with_echo,
            permissions=gate,
            audit_logger=logger,
        )

        assert final.status == "blocked"

        # Turn should be recorded with empty tool executions
        turn_dir = logger.run_dir / "turn_000"
        assert turn_dir.exists()
        tools_data = json.loads((turn_dir / "tool_executions.json").read_text())
        assert tools_data == []

        manifest = json.loads((logger.run_dir / "run.json").read_text())
        assert manifest["final_status"] == "blocked"
