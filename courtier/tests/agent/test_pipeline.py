"""Tests for the unified pipeline (GuardrailSystem v2): interceptors, observers, scopes."""

from __future__ import annotations

import asyncio

import pytest

from courtier.agent.core.guardrails import (
    GuardContext,
    GuardResult,
    GuardrailSystem,
)
from courtier.agent.core.guardrails.guardrail_system import SCOPES


def _ctx(state=None):
    return GuardContext(state=state if state is not None else object())


class _BlockingInputGuard:
    name = "block_input"
    layer = "input"

    async def check(self, context: GuardContext) -> GuardResult:
        return GuardResult.block(self.name, "input blocked")


class TestInterceptorRegistration:
    def test_tool_call_scope_rejects_interceptors(self):
        system = GuardrailSystem()

        async def handler(ctx):
            return None

        with pytest.raises(ValueError, match="decision-only"):
            system.register_interceptor("tool_call", handler)

    def test_unknown_scope_rejected(self):
        system = GuardrailSystem()

        async def handler(ctx):
            return None

        with pytest.raises(ValueError, match="unknown pipeline scope"):
            system.register_interceptor("nowhere", handler)
        with pytest.raises(ValueError, match="unknown pipeline scope"):
            system.register_observer("nowhere", handler)


class TestRunScope:
    @pytest.mark.asyncio
    async def test_interceptors_run_before_guards_and_thread_state(self):
        """适配 → 裁决：拦截器改写的状态必须是被裁决的状态。"""
        seen_by_guard: list[str] = []

        class _Spy:
            name = "spy"
            layer = "input"

            async def check(self, context: GuardContext) -> GuardResult:
                seen_by_guard.append(context.state)
                return GuardResult.allow(self.name)

        async def inject(context: GuardContext):
            return "adapted-state"

        system = GuardrailSystem()
        system.register_interceptor("pre_think", inject)
        system.register(_Spy())
        outcome = await system.run_scope("pre_think", _ctx("initial"))
        assert seen_by_guard == ["adapted-state"]
        assert outcome.state == "adapted-state"
        assert outcome.blocked is None

    @pytest.mark.asyncio
    async def test_interceptor_priority_order(self):
        calls: list[str] = []

        def make(name):
            async def handler(context):
                calls.append(name)
                return None

            return handler

        system = GuardrailSystem()
        system.register_interceptor("pre_think", make("low"), priority=0)
        system.register_interceptor("pre_think", make("high"), priority=10)
        await system.run_scope("pre_think", _ctx())
        assert calls == ["high", "low"]

    @pytest.mark.asyncio
    async def test_interceptor_exception_is_fail_open(self):
        calls: list[str] = []

        async def boom(context):
            raise RuntimeError("crash")

        async def after(context):
            calls.append("after")
            return None

        system = GuardrailSystem()
        system.register_interceptor("pre_think", boom)
        system.register_interceptor("pre_think", after)
        outcome = await system.run_scope("pre_think", _ctx("s"))
        assert calls == ["after"]
        assert outcome.state == "s"

    @pytest.mark.asyncio
    async def test_interceptor_timeout_is_fail_open(self):
        async def slow(context):
            await asyncio.sleep(5)
            return "late"

        system = GuardrailSystem()
        system.register_interceptor("pre_think", slow, timeout=0.01)
        outcome = await system.run_scope("pre_think", _ctx("s"))
        assert outcome.state == "s"

    @pytest.mark.asyncio
    async def test_blocked_guard_surfaces_in_outcome(self):
        system = GuardrailSystem()
        system.register(_BlockingInputGuard())
        outcome = await system.run_scope("pre_think", _ctx())
        assert outcome.blocked is not None
        assert outcome.blocked.guard_name == "block_input"

    @pytest.mark.asyncio
    async def test_observer_runs_after_guards_and_swallows_errors(self):
        events: list[str] = []

        class _Allow:
            name = "allow_guard"
            layer = "input"

            async def check(self, context: GuardContext) -> GuardResult:
                events.append("guard")
                return GuardResult.allow(self.name)

        async def bad_observer(context):
            events.append("observer")
            raise RuntimeError("observer crash")

        system = GuardrailSystem()
        system.register(_Allow())
        system.register_observer("pre_think", bad_observer)
        outcome = await system.run_scope("pre_think", _ctx())
        assert events == ["guard", "observer"]
        assert outcome.blocked is None

    @pytest.mark.asyncio
    async def test_set_context_injected_into_dispatch(self):
        seen: list[GuardContext] = []

        async def observer(context: GuardContext):
            seen.append(context)

        system = GuardrailSystem()
        system.set_context(agent_name="orch", session_id="s1", extra="e")
        system.register_observer("pre_think", observer)
        await system.run_scope("pre_think", _ctx())
        assert seen[0].agent_name == "orch"
        assert seen[0].session_id == "s1"
        assert seen[0].metadata["extra"] == "e"

    @pytest.mark.asyncio
    async def test_aggregate_merges_allow_metadata(self):
        """全 allow 时聚合携带各 guard 的 metadata（严格度只决定动作，不丢数据）。"""
        class _Meta:
            name = "meta_guard"
            layer = "post_tool"

            async def check(self, context: GuardContext) -> GuardResult:
                return GuardResult.allow(
                    self.name, metadata={"consecutive_exploratory": 3}
                )

        system = GuardrailSystem()
        system.register(_Meta())
        outcome = await system.run_scope("post_tool", _ctx())
        assert outcome.guard_result.action == "allow"
        assert outcome.guard_result.metadata["consecutive_exploratory"] == 3
        assert outcome.blocked is None

    def test_scope_table_covers_all_layers(self):
        assert set(SCOPES) == {"pre_think", "output", "tool", "tool_call", "post_tool"}
        assert SCOPES["pre_think"] == "input"
        assert SCOPES["post_tool"] == "post_tool"
