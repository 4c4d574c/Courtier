"""Tests for GuardrailSystem.check_call — per-call fail-closed dispatch."""

import pytest

from courtier.agent.core.guardrails import (
    CallGuardResult,
    GuardContext,
    GuardrailSystem,
    GuardResult,
)
from courtier.agent.core.tool_call import ToolCall


class _DenyGuard:
    name = "deny_guard"
    layer = "tool_call"

    async def check_call(self, call: ToolCall, context: GuardContext) -> CallGuardResult:
        return CallGuardResult.deny(self.name, "denied by policy")


class _AllowGuard:
    name = "allow_guard"
    layer = "tool_call"

    async def check_call(self, call: ToolCall, context: GuardContext) -> CallGuardResult:
        return CallGuardResult.allow(self.name)


class _ConfirmGuard:
    name = "confirm_guard"
    layer = "tool_call"

    async def check_call(self, call: ToolCall, context: GuardContext) -> CallGuardResult:
        return CallGuardResult.confirm(self.name, "please confirm")


class _BoomGuard:
    name = "boom_guard"
    layer = "tool_call"
    async def check_call(self, call: ToolCall, context: GuardContext) -> CallGuardResult:
        raise RuntimeError("guard crashed")


class _TurnGuard:
    """Layer guard without check_call — must not participate in tool_call."""

    name = "turn_guard"
    layer = "tool_call"

    async def check(self, context: GuardContext) -> GuardResult:
        return GuardResult.block(self.name, "turn blocked")


def _call(name: str = "echo") -> ToolCall:
    return ToolCall(id="1", name=name, arguments={})


@pytest.mark.asyncio
async def test_allow_when_no_call_guards():
    system = GuardrailSystem()
    result = await system.check_call("tool_call", _call(), GuardContext())
    assert result.action == "allow"


@pytest.mark.asyncio
async def test_first_deny_wins_short_circuit():
    system = GuardrailSystem()
    system.register(_AllowGuard())
    system.register(_DenyGuard())
    system.register(_BoomGuard())  # must never run — short-circuited
    result = await system.check_call("tool_call", _call(), GuardContext())
    assert result.action == "deny"
    assert result.guard_name == "deny_guard"
    assert result.error_code == "permission_denied"


@pytest.mark.asyncio
async def test_guard_exception_fails_closed_in_block_mode():
    system = GuardrailSystem()
    system.register(_BoomGuard())
    result = await system.check_call("tool_call", _call(), GuardContext())
    assert result.action == "deny"
    assert result.error_code == "permission_error"
    assert result.guard_name == "boom_guard"


@pytest.mark.asyncio
async def test_shadow_mode_records_but_never_enforces():
    events: list[GuardResult] = []

    async def on_event(result: GuardResult) -> None:
        events.append(result)

    system = GuardrailSystem(tool_call_mode="log", on_event=on_event)
    system.register(_DenyGuard())
    system.register(_BoomGuard())
    result = await system.check_call("tool_call", _call(), GuardContext())
    assert result.action == "allow"
    assert any(e.guard_name == "deny_guard" and e.action == "block" for e in events)


@pytest.mark.asyncio
async def test_off_mode_skips_entirely():
    system = GuardrailSystem(tool_call_mode="off")
    system.register(_BoomGuard())
    result = await system.check_call("tool_call", _call(), GuardContext())
    assert result.action == "allow"


@pytest.mark.asyncio
async def test_guard_without_check_call_does_not_participate():
    system = GuardrailSystem()
    system.register(_TurnGuard())
    result = await system.check_call("tool_call", _call(), GuardContext())
    assert result.action == "allow"


@pytest.mark.asyncio
async def test_confirm_passes_through_to_caller():
    system = GuardrailSystem()
    system.register(_ConfirmGuard())
    result = await system.check_call("tool_call", _call(), GuardContext())
    assert result.action == "confirm"
    assert result.reason == "please confirm"


@pytest.mark.asyncio
async def test_confirm_downgraded_in_shadow_mode():
    system = GuardrailSystem(tool_call_mode="log")
    system.register(_ConfirmGuard())
    result = await system.check_call("tool_call", _call(), GuardContext())
    assert result.action == "allow"


@pytest.mark.asyncio
async def test_deny_records_metric_and_mirrored_event():
    from unittest.mock import patch

    events: list[GuardResult] = []

    async def on_event(result: GuardResult) -> None:
        events.append(result)

    system = GuardrailSystem(on_event=on_event)
    system.register(_DenyGuard())
    with patch("courtier.agent.core.guardrails.guardrail_system.record_guardrail_blocked") as rec:
        await system.check_call("tool_call", _call(), GuardContext())
    rec.assert_called_once_with(layer="tool_call", guard_name="deny_guard")
    mirrored = events[-1]
    assert mirrored.action == "block"
    assert mirrored.layer == "tool_call"
    assert mirrored.metadata.get("error_code") == "permission_denied"


class _SpyGuard:
    name = "spy_guard"
    layer = "tool_call"

    def __init__(self) -> None:
        self.contexts: list[GuardContext] = []

    async def check_call(self, call: ToolCall, context: GuardContext) -> CallGuardResult:
        self.contexts.append(context)
        return CallGuardResult.allow(self.name)


@pytest.mark.asyncio
async def test_check_call_injects_session_identity():
    """循环传入裸上下文，会话身份由系统注入（wiring-plan T2/D3）。"""
    spy = _SpyGuard()
    system = GuardrailSystem(tool_call_mode="block")
    system.register(spy)
    system.set_context(agent_name="Orchestrator", session_id="sess-1")

    decision = await system.check_call(
        "tool_call", _call(), GuardContext(state=object())
    )

    assert decision.action == "allow"
    assert spy.contexts[0].agent_name == "Orchestrator"
    assert spy.contexts[0].session_id == "sess-1"


@pytest.mark.asyncio
async def test_empty_identity_does_not_override_caller():
    """系统未 set_context 时，不覆盖调用方自带的身份字段。"""
    spy = _SpyGuard()
    system = GuardrailSystem(tool_call_mode="block")
    system.register(spy)

    context = GuardContext(state=object(), agent_name="caller", session_id="s0")
    await system.check_call("tool_call", _call(), context)

    assert context.agent_name == "caller"
    assert context.session_id == "s0"
