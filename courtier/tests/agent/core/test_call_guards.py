"""Tests for the per-call guard vocabulary (CallGuardResult / CallGuardrail)."""

from courtier.agent.core.guardrails import CallGuardResult, GuardContext
from courtier.agent.core.tool_call import ToolCall


class _AllowCallGuard:
    name = "allow_call"

    async def check_call(self, call: ToolCall, context: GuardContext) -> CallGuardResult:
        return CallGuardResult.allow(self.name)


class _DenyCallGuard:
    name = "deny_call"

    async def check_call(self, call: ToolCall, context: GuardContext) -> CallGuardResult:
        return CallGuardResult.deny(self.name, "not allowed", error_code="permission_denied")


class _ConfirmCallGuard:
    name = "confirm_call"

    async def check_call(self, call: ToolCall, context: GuardContext) -> CallGuardResult:
        return CallGuardResult.confirm(self.name, "please confirm")


class _TurnOnlyGuard:
    """A guard without check_call — must not participate in the tool_call layer."""

    name = "turn_only"
    layer = "tool"

    async def check(self, context: GuardContext):
        raise AssertionError("turn layer check must not be consulted here")


def test_deny_result_carries_error_code():
    result = CallGuardResult.deny("g", "reason")
    assert result.action == "deny"
    assert result.error_code == "permission_denied"


def test_confirm_result_is_vocabulary_reservation():
    result = CallGuardResult.confirm("g", "please confirm")
    assert result.action == "confirm"
    assert result.reason == "please confirm"


def test_guard_without_check_call_is_not_call_participant():
    assert not hasattr(_TurnOnlyGuard(), "check_call")
    assert hasattr(_AllowCallGuard(), "check_call")


def test_call_guards_receive_tool_call_and_context():
    import asyncio

    call = ToolCall(id="1", name="echo", arguments={})
    ctx = GuardContext()

    async def _run():
        return await _DenyCallGuard().check_call(call, ctx)

    result = asyncio.run(_run())
    assert result.action == "deny"
    assert result.reason == "not allowed"
