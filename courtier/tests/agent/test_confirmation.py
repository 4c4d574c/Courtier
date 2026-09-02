"""Confirmation chain: guard, loop branch, RunManager bridge."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from courtier.agent.core.execution_result import ExecutionResult
from courtier.agent.core.guardrails import ConfirmationGuard, GuardrailSystem
from courtier.agent.core.guardrails.confirmation import ConfirmationGuard as CG
from courtier.agent.core.loop_phases import execute_tools_phase
from courtier.agent.core.tool_call import ToolCall
from courtier.config import ToolConfirmationRule


def _rule(tool: str, message: str = "") -> ToolConfirmationRule:
    return ToolConfirmationRule(tool=tool, message=message)


class _EchoTool:
    name = "echo"
    description = "echo"
    parameters = {"type": "object", "properties": {"text": {"type": "string"}}}

    async def execute(self, **kwargs):
        return ExecutionResult(
            success=True,
            actor_type="tool",
            actor_name="echo",
            raw_data={"echoed": kwargs.get("text", "")},
        )


class _Registry:
    def __init__(self):
        self.tool = _EchoTool()

    async def execute(self, name, **kwargs):
        return await self.tool.execute(**kwargs)


def _system(rules=None, approved=None) -> GuardrailSystem:
    system = GuardrailSystem()
    system.register(ConfirmationGuard(rules=rules or [], approved_tools=approved))
    return system


class TestConfirmationGuard:
    @pytest.mark.asyncio
    async def test_listed_tool_confirms_with_message(self):
        guard = CG(rules=[_rule("deploy", "确认部署？")])
        result = await guard.check_call(ToolCall(id="1", name="deploy", arguments={}), None)
        assert result.action == "confirm"
        assert result.reason == "确认部署？"
        assert result.metadata["tool"] == "deploy"

    @pytest.mark.asyncio
    async def test_default_message_when_empty(self):
        guard = CG(rules=[_rule("deploy")])
        result = await guard.check_call(ToolCall(id="1", name="deploy", arguments={}), None)
        assert "deploy" in result.reason

    @pytest.mark.asyncio
    async def test_unlisted_tool_allows(self):
        guard = CG(rules=[_rule("deploy")])
        result = await guard.check_call(ToolCall(id="1", name="echo", arguments={}), None)
        assert result.action == "allow"

    @pytest.mark.asyncio
    async def test_session_approved_short_circuits(self):
        approved: set[str] = set()
        guard = CG(rules=[_rule("deploy")], approved_tools=approved)
        before = await guard.check_call(ToolCall(id="1", name="deploy", arguments={}), None)
        assert before.action == "confirm"
        approved.add("deploy")
        after = await guard.check_call(ToolCall(id="1", name="deploy", arguments={}), None)
        assert after.action == "allow"


def _call() -> ToolCall:
    return ToolCall(id="c1", name="deploy", arguments={})


class _State:
    tool_calls = [_call()]


class TestToolPhaseConfirmBranch:
    @pytest.mark.asyncio
    async def test_approved_call_dispatches(self):
        async def handler(call, message):
            return True

        results, records = await execute_tools_phase(
            state=_State(),
            tool_registry=_Registry(),
            context_manager=None,
            artifact_store=None,
            on_tool_result=None,
            guardrail_system=_system(rules=[_rule("deploy")]),
            confirmation_handler=handler,
        )
        assert len(results) == 1 and results[0].success

    @pytest.mark.asyncio
    async def test_denied_call_yields_confirmation_denied(self):
        async def handler(call, message):
            return False

        results, records = await execute_tools_phase(
            state=_State(),
            tool_registry=_Registry(),
            context_manager=None,
            artifact_store=None,
            on_tool_result=None,
            guardrail_system=_system(rules=[_rule("deploy")]),
            confirmation_handler=handler,
        )
        assert len(results) == 1 and not results[0].success
        assert records[0].result_error is not None
        assert "拒绝" in (results[0].error or "")

    @pytest.mark.asyncio
    async def test_missing_handler_fails_closed(self):
        results, _ = await execute_tools_phase(
            state=_State(),
            tool_registry=_Registry(),
            context_manager=None,
            artifact_store=None,
            on_tool_result=None,
            guardrail_system=_system(rules=[_rule("deploy")]),
            confirmation_handler=None,
        )
        assert not results[0].success


class TestRunManagerBridge:
    def _manager_with_run(self):
        from courtier.agent.api.services.run_manager import AgentRun, RunManager
        from courtier.agent.core.event_bus import EventBus
        from courtier.agent.api.services.run_event_log import RunEventLog

        store = SimpleNamespace()
        settings = SimpleNamespace(
            run_grace_seconds=600,
            run_log_max_events=50_000,
            run_log_max_bytes=8 * 1024 * 1024,
            max_runs_per_user=3,
            max_total_runs=20,
        )
        manager = RunManager(session_store=store, settings=settings)
        run = AgentRun("sess1", "user1", RunEventLog(), EventBus())
        manager._runs["sess1"] = run  # bypass start(): unit-scoped bridge test
        return manager, run

    @pytest.mark.asyncio
    async def test_handler_roundtrip_and_events(self):
        manager, run = self._manager_with_run()
        guard = CG(rules=[_rule("deploy")])
        handler = manager._confirmation_handler(run, guard)
        task = asyncio.create_task(handler(ToolCall(id="c1", name="deploy", arguments={}), "msg"))
        await asyncio.sleep(0)
        pending = manager.list_pending_confirmations("sess1")
        assert len(pending) == 1 and pending[0]["toolName"] == "deploy"
        cid = pending[0]["confirmationId"]
        assert manager.pending_confirmation_count("sess1") == 1
        outcome = manager.resolve_confirmation("sess1", cid, "approve")
        assert outcome["decision"] == "approve" and outcome["toolName"] == "deploy"
        assert await task is True
        assert manager.pending_confirmation_count("sess1") == 0
        types = [e.payload["type"] for e in run.log._entries]
        assert "confirmation_requested" in types and "confirmation_resolved" in types

    @pytest.mark.asyncio
    async def test_approve_session_feeds_sink(self):
        manager, run = self._manager_with_run()
        guard = CG(rules=[_rule("deploy")])
        run.confirmation_sink = guard.approve_session
        handler = manager._confirmation_handler(run, guard)
        task = asyncio.create_task(handler(ToolCall(id="c1", name="deploy", arguments={}), "m"))
        await asyncio.sleep(0)
        cid = manager.list_pending_confirmations("sess1")[0]["confirmationId"]
        manager.resolve_confirmation("sess1", cid, "approve_session")
        assert await task is True
        assert "deploy" in guard.approved_tools

    @pytest.mark.asyncio
    async def test_deny_resolves_false(self):
        manager, run = self._manager_with_run()
        handler = manager._confirmation_handler(run, CG(rules=[_rule("deploy")]))
        task = asyncio.create_task(handler(ToolCall(id="c1", name="deploy", arguments={}), "m"))
        await asyncio.sleep(0)
        cid = manager.list_pending_confirmations("sess1")[0]["confirmationId"]
        manager.resolve_confirmation("sess1", cid, "deny")
        assert await task is False

    @pytest.mark.asyncio
    async def test_resolve_unknown_and_idempotent(self):
        manager, run = self._manager_with_run()
        assert manager.resolve_confirmation("sess1", "nope", "approve") is None
        assert manager.list_pending_confirmations("sess1") == []

        handler = manager._confirmation_handler(run, CG(rules=[_rule("deploy")]))
        task = asyncio.create_task(handler(ToolCall(id="c1", name="deploy", arguments={}), "m"))
        await asyncio.sleep(0)
        cid = manager.list_pending_confirmations("sess1")[0]["confirmationId"]
        first = manager.resolve_confirmation("sess1", cid, "deny")
        assert first["decision"] == "deny"
        second = manager.resolve_confirmation("sess1", cid, "deny")
        assert second["alreadyResolved"] is True
        assert await task is False

    def test_invalid_decision_rejected(self):
        manager, run = self._manager_with_run()
        with pytest.raises(ValueError):
            manager.resolve_confirmation("sess1", "cid", "maybe")
