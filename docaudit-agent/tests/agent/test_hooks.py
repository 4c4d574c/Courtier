"""Tests for HookChain and PermissionGate."""

import asyncio
import logging

import pytest

from courtier.agent.core.state import AgentState
from courtier.agent.hooks.chain import (
    HookChain,
    HookHandle,
    HookObserver,
    POST_OBSERVE,
    PRE_SEARCH,
    PRE_THINK,
)
from courtier.agent.permissions.gate import PermissionGate
from courtier.agent.core.model import ToolCall


class TestHookChain:
    @pytest.mark.asyncio
    async def test_no_handlers_returns_state_unchanged(self):
        chain = HookChain()
        state = AgentState.initial(task="test")
        result = await chain.run("pre_think", state)
        assert result is state

    @pytest.mark.asyncio
    async def test_handler_runs_and_modifies_state(self):
        chain = HookChain()
        state = AgentState.initial(task="test")

        async def append_note(ctx):
            return ctx.state.model_copy(update={"termination_reason": "hook ran"})

        chain.register("pre_think", append_note)
        result = await chain.run("pre_think", state)
        assert result.termination_reason == "hook ran"

    @pytest.mark.asyncio
    async def test_multiple_handlers_run_in_order(self):
        chain = HookChain()
        state = AgentState.initial(task="test")
        order: list[str] = []

        async def first(ctx):
            order.append("first")
            return ctx.state

        async def second(ctx):
            order.append("second")
            return ctx.state

        chain.register("test_event", first)
        chain.register("test_event", second)
        await chain.run("test_event", state)
        assert order == ["first", "second"]

    @pytest.mark.asyncio
    async def test_different_events_independent(self):
        chain = HookChain()
        state = AgentState.initial(task="test")

        async def handler_a(ctx):
            return ctx.state.model_copy(update={"termination_reason": "a"})

        async def handler_b(ctx):
            return ctx.state.model_copy(update={"termination_reason": "b"})

        chain.register("event_a", handler_a)
        chain.register("event_b", handler_b)

        result_a = await chain.run("event_a", state)
        result_b = await chain.run("event_b", state)
        assert result_a.termination_reason == "a"
        assert result_b.termination_reason == "b"

    # — Phase 1.1: exception handling ————————————————————————————

    @pytest.mark.asyncio
    async def test_handler_exception_continues_by_default(self):
        """Exception in one handler does not prevent subsequent handlers."""
        chain = HookChain()
        state = AgentState.initial(task="test")
        second_ran = False

        async def failing_handler(ctx):
            raise RuntimeError("handler error")

        async def second_handler(ctx):
            nonlocal second_ran
            second_ran = True
            return ctx.state

        chain.register("test_event", failing_handler)
        chain.register("test_event", second_handler)
        result = await chain.run("test_event", state)
        assert second_ran
        # State is still returned (from second_handler via pipeline)
        assert result is not None

    @pytest.mark.asyncio
    async def test_handler_exception_strict_mode_raises(self):
        """continue_on_error=False propagates the exception."""
        chain = HookChain(continue_on_error=False)
        state = AgentState.initial(task="test")

        async def failing_handler(ctx):
            raise RuntimeError("handler error")

        chain.register("test_event", failing_handler)
        with pytest.raises(RuntimeError, match="handler error"):
            await chain.run("test_event", state)

    @pytest.mark.asyncio
    async def test_handler_exception_per_run_override(self):
        """Per-run continue_on_error overrides the chain-level default."""
        chain = HookChain(continue_on_error=True)
        state = AgentState.initial(task="test")

        async def failing_handler(ctx):
            raise RuntimeError("handler error")

        chain.register("test_event", failing_handler)
        # Per-run override to strict mode should raise
        with pytest.raises(RuntimeError, match="handler error"):
            await chain.run("test_event", state, continue_on_error=False)

    # — Phase 1.2: observer (fire-and-forget) ——————————————————————

    @pytest.mark.asyncio
    async def test_observer_runs_fire_and_forget(self):
        """Observer registered via register_observer runs."""
        chain = HookChain()
        state = AgentState.initial(task="test")
        observer_ran = False

        async def my_observer(ctx):
            nonlocal observer_ran
            observer_ran = True

        chain.register_observer("test_event", my_observer)
        result = await chain.run("test_event", state)
        assert observer_ran
        # Observer does not affect state
        assert result is state

    @pytest.mark.asyncio
    async def test_observer_and_interceptor_run_together(self):
        """Interceptors run first, then observers."""
        chain = HookChain()
        state = AgentState.initial(task="test")
        order: list[str] = []

        async def interceptor(ctx):
            order.append("interceptor")
            return ctx.state.model_copy(update={"termination_reason": "from_interceptor"})

        async def observer(ctx):
            order.append("observer")

        chain.register("test_event", interceptor)
        chain.register_observer("test_event", observer)
        result = await chain.run("test_event", state)
        assert order == ["interceptor", "observer"]
        assert result.termination_reason == "from_interceptor"

    @pytest.mark.asyncio
    async def test_observer_exception_does_not_break_chain(self):
        """Observer exception is caught and logged; subsequent observers run."""
        chain = HookChain()
        state = AgentState.initial(task="test")
        second_ran = False

        async def failing_observer(ctx):
            raise RuntimeError("observer error")

        async def second_observer(ctx):
            nonlocal second_ran
            second_ran = True

        chain.register_observer("test_event", failing_observer)
        chain.register_observer("test_event", second_observer)
        await chain.run("test_event", state)
        assert second_ran

    # — Phase 2.3: HookEvent type safety ———————————————————————————

    def test_hook_event_constants_are_strings(self):
        """PRE_THINK, POST_OBSERVE, PRE_SEARCH are plain strings."""
        assert PRE_THINK == "pre_think"
        assert POST_OBSERVE == "post_observe"
        assert PRE_SEARCH == "pre_search"

    def test_hook_event_works_as_dict_key(self):
        """HookEvent constants work as dict keys (they are str at runtime)."""
        chain = HookChain()
        handle = chain.register(PRE_THINK, _make_noop_handler())
        # HookEvent is a NewType over str; registering should succeed and return a handle
        assert isinstance(handle, HookHandle)
    # — Phase 2.4: cancelable handles —————————————————————————————

    @pytest.mark.asyncio
    async def test_register_returns_cancelable_handle(self):
        """handle.cancel() removes the handler from the chain."""
        chain = HookChain()
        state = AgentState.initial(task="test")

        async def handler(ctx):
            return ctx.state.model_copy(update={"termination_reason": "should_not_run"})

        handle = chain.register("test_event", handler)
        handle.cancel()
        result = await chain.run("test_event", state)
        assert result.termination_reason is None

    @pytest.mark.asyncio
    async def test_cancel_removes_only_target_handler(self):
        """Canceling one handler leaves others intact."""
        chain = HookChain()
        state = AgentState.initial(task="test")
        order: list[str] = []

        async def first(ctx):
            order.append("first")
            return ctx.state

        async def second(ctx):
            order.append("second")
            return ctx.state

        h1 = chain.register("test_event", first)
        h2 = chain.register("test_event", second)
        h1.cancel()
        await chain.run("test_event", state)
        assert order == ["second"]

        h2.cancel()
        await chain.run("test_event", state)
        assert order == ["second"]  # second no longer runs

    @pytest.mark.asyncio
    async def test_cancel_is_idempotent(self):
        """Calling cancel() twice does not raise."""
        chain = HookChain()
        handle = chain.register("test_event", _make_noop_handler())
        handle.cancel()
        handle.cancel()  # should not raise

    @pytest.mark.asyncio
    async def test_cancel_observer(self):
        """handle.cancel() also works for observers."""
        chain = HookChain()
        state = AgentState.initial(task="test")
        observer_ran = False

        async def observer(ctx):
            nonlocal observer_ran
            observer_ran = True

        handle = chain.register_observer("test_event", observer)
        handle.cancel()
        await chain.run("test_event", state)
        assert not observer_ran

    # — Phase 2.5: priority ————————————————————————————————————————

    @pytest.mark.asyncio
    async def test_priority_order(self):
        """Higher priority handlers run first."""
        chain = HookChain()
        state = AgentState.initial(task="test")
        order: list[int] = []

        def make_handler(n: int):
            async def handler(ctx):
                order.append(n)
                return ctx.state
            return handler

        chain.register("test_event", make_handler(3), priority=3)
        chain.register("test_event", make_handler(1), priority=1)
        chain.register("test_event", make_handler(5), priority=5)
        await chain.run("test_event", state)
        assert order == [5, 3, 1]

    @pytest.mark.asyncio
    async def test_priority_stable_sort(self):
        """Equal priority preserves registration order."""
        chain = HookChain()
        state = AgentState.initial(task="test")
        order: list[str] = []

        async def a(ctx): order.append("a"); return ctx.state
        async def b(ctx): order.append("b"); return ctx.state
        async def c(ctx): order.append("c"); return ctx.state

        chain.register("test_event", a, priority=0)
        chain.register("test_event", b, priority=0)
        chain.register("test_event", c, priority=0)
        await chain.run("test_event", state)
        assert order == ["a", "b", "c"]

    # — Phase 2.6: extended HookContext ————————————————————————————

    @pytest.mark.asyncio
    async def test_hook_context_has_extended_fields(self):
        """set_context() populates agent_name, session_id, and metadata."""
        chain = HookChain()
        chain.set_context(
            agent_name="TestAgent",
            session_id="sess-123",
            custom_key="custom_value",
        )
        state = AgentState.initial(task="test")
        ctx_data = {}

        async def capture(ctx):
            ctx_data["agent_name"] = ctx.agent_name
            ctx_data["session_id"] = ctx.session_id
            ctx_data["current_step"] = ctx.current_step
            ctx_data["custom_key"] = ctx.metadata.get("custom_key")
            return ctx.state

        chain.register("test_event", capture)
        await chain.run("test_event", state)
        assert ctx_data["agent_name"] == "TestAgent"
        assert ctx_data["session_id"] == "sess-123"
        assert ctx_data["current_step"] == 0
        assert ctx_data["custom_key"] == "custom_value"

    @pytest.mark.asyncio
    async def test_hook_context_defaults_are_empty(self):
        """Without set_context(), extended fields are empty."""
        chain = HookChain()
        state = AgentState.initial(task="test")
        ctx_data = {}

        async def capture(ctx):
            ctx_data["agent_name"] = ctx.agent_name
            ctx_data["metadata"] = ctx.metadata
            return ctx.state

        chain.register("test_event", capture)
        await chain.run("test_event", state)
        assert ctx_data["agent_name"] == ""
        assert ctx_data["metadata"] == {}

    # — Phase 2.7: handler timeout —————————————————————————————————

    @pytest.mark.asyncio
    async def test_handler_timeout_logs_warning_and_continues(self, caplog):
        """Timeout is caught, logged, and subsequent handlers run."""
        caplog.set_level(logging.WARNING)
        chain = HookChain(handler_timeout=0.01)
        state = AgentState.initial(task="test")
        second_ran = False

        async def slow_handler(ctx):
            await asyncio.sleep(1.0)
            return ctx.state

        async def second_handler(ctx):
            nonlocal second_ran
            second_ran = True
            return ctx.state

        chain.register("test_event", slow_handler)
        chain.register("test_event", second_handler)
        await chain.run("test_event", state)
        assert second_ran
        assert any("timed out" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_per_handler_timeout_overrides_chain_default(self):
        """Per-handler timeout takes precedence over chain-level default."""
        chain = HookChain(handler_timeout=5.0)  # chain-level, generous
        state = AgentState.initial(task="test")
        second_ran = False

        async def slow_handler(ctx):
            await asyncio.sleep(1.0)
            return ctx.state

        async def second_handler(ctx):
            nonlocal second_ran
            second_ran = True
            return ctx.state

        chain.register("test_event", slow_handler, timeout=0.01)  # per-handler, tight
        chain.register("test_event", second_handler)
        await chain.run("test_event", state)
        assert second_ran

    @pytest.mark.asyncio
    async def test_handler_timeout_strict_mode_raises(self):
        """Timeout with continue_on_error=False propagates asyncio.TimeoutError."""
        chain = HookChain(handler_timeout=0.01, continue_on_error=False)
        state = AgentState.initial(task="test")

        async def slow_handler(ctx):
            await asyncio.sleep(1.0)
            return ctx.state

        chain.register("test_event", slow_handler)
        with pytest.raises(asyncio.TimeoutError):
            await chain.run("test_event", state)


def _make_noop_handler():
    """Factory: returns a no-op interceptor that passes state through."""
    async def noop(ctx):
        return ctx.state
    return noop


class TestPermissionGate:
    def test_default_allows_all(self):
        gate = PermissionGate()
        tc = ToolCall(id="1", name="any_tool", arguments={})
        assert gate.allow(tc) is True

    def test_blocked_tool_denied(self):
        gate = PermissionGate()
        gate.block("delete_rule")
        tc = ToolCall(id="1", name="delete_rule", arguments={})
        assert gate.allow(tc) is False

    def test_require_confirmation_still_allows(self):
        gate = PermissionGate()
        gate.require_confirmation("delete_rule", "Are you sure?")
        tc = ToolCall(id="1", name="delete_rule", arguments={})
        # Confirmation is a soft gate — allow for now
        assert gate.allow(tc) is True

    def test_unrelated_tool_allowed_after_block(self):
        gate = PermissionGate()
        gate.block("delete_rule")
        assert gate.allow(ToolCall(id="1", name="delete_rule", arguments={})) is False
        assert gate.allow(ToolCall(id="2", name="echo", arguments={})) is True

    def test_needs_confirmation_returns_message(self):
        gate = PermissionGate()
        gate.require_confirmation("delete_rule", "Are you sure?")
        needs, msg = gate.needs_confirmation("delete_rule")
        assert needs is True
        assert msg == "Are you sure?"

    def test_needs_confirmation_returns_false_for_unregistered(self):
        gate = PermissionGate()
        needs, msg = gate.needs_confirmation("echo")
        assert needs is False
        assert msg == ""
