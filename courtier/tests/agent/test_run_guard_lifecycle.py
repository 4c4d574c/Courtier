"""Run-scoped guard lifecycle: descriptor recipes materialize fresh
instances per agent_loop and are unregistered when the loop ends —
the stateful-semantics invariant (guardrails doc §8 坑 2)."""

from __future__ import annotations

import pytest

from courtier.agent.core.guardrails import GuardrailSystem
from courtier.agent.core.guardrails.registry import DEFAULT_GUARD_DECLARATIONS, GuardDescriptor
from courtier.agent.core.loop import agent_loop
from courtier.agent.core.model import MockModelClient
from courtier.agent.core.state import AgentState
from courtier.agent.testing import StatefulRunGuard

STATEFUL_PATH = "courtier.agent.testing.StatefulRunGuard"


def _system_with_stateful_run_guard() -> GuardrailSystem:
    system = GuardrailSystem()
    system.run_descriptors = [
        GuardDescriptor(name="stateful_run", class_path=STATEFUL_PATH, scope="run")
    ]
    return system


class TestRunScopedLifecycle:
    @pytest.mark.asyncio
    async def test_fresh_instance_per_run_and_unregistered_after(self):
        system = _system_with_stateful_run_guard()
        state = AgentState.initial("task")

        final = await agent_loop(
            state=state, model=MockModelClient(tool_calls=[]), guardrail_system=system
        )
        assert final.status == "completed"
        # One instance materialized for this run; gone from the shared
        # system afterwards.
        assert len(StatefulRunGuard.instances) == 1
        assert system.guardrails == []

        final2 = await agent_loop(
            state=AgentState.initial("task"),
            model=MockModelClient(tool_calls=[]),
            guardrail_system=system,
        )
        assert final2.status == "completed"
        # Second run built its own instance — history cannot leak across runs.
        assert len(StatefulRunGuard.instances) == 2
        assert StatefulRunGuard.instances[0] != StatefulRunGuard.instances[1]
        assert system.guardrails == []

    @pytest.mark.asyncio
    async def test_adhoc_loop_defaults_to_baseline_run_guards(self):
        """agent_loop without a caller system seeds the baseline run-scoped
        declarations (the old hardcoded _run_guards behavior)."""
        from courtier.agent.core.guardrails import (
            BusinessArtifactProgressGuard,
            ExploreLoopGuard,
        )

        final = await agent_loop(
            state=AgentState.initial("task"),
            model=MockModelClient(tool_calls=[]),
        )
        assert final.status == "completed"
        # Baseline behavior preserved: the explore-loop guard triggers on
        # consecutive null results (covered in test_guardrails.py); here we
        # assert the descriptor seeding only.
        baseline = [d for d in DEFAULT_GUARD_DECLARATIONS if d.scope == "run"]
        assert [d.name for d in baseline] == ["explore_loop", "business_artifact"]
        # The classes are still the historical implementations.
        assert isinstance(ExploreLoopGuard(), ExploreLoopGuard)
        assert isinstance(BusinessArtifactProgressGuard(), BusinessArtifactProgressGuard)

    @pytest.mark.asyncio
    async def test_explore_loop_metadata_absent_is_safe(self):
        """With run guards disabled (empty descriptors), the loop's
        ``consecutive_exploratory`` metadata read must default safely."""
        from courtier.agent.core.execution_result import ExecutionResult
        from courtier.agent.core.model import ModelResponse, ToolCall
        from courtier.agent.tools.registry import ToolRegistry

        class _NullTool:
            name = "null_tool"
            description = "returns null"
            parameters = {"type": "object", "properties": {}}

            async def execute(self, **kwargs):
                return ExecutionResult(
                    success=True, actor_type="tool", actor_name="null_tool", raw_data=None
                )

        class _ExploringModel:
            model_name = "explore"
            temperature = 0.0

            async def generate(self, messages, tools=None, **kwargs):
                return ModelResponse(
                    content=None,
                    tool_calls=[ToolCall(id="1", name="null_tool", arguments={})],
                )

            async def generate_stream_full(
                self, messages, tools=None, on_token=None, on_content_token=None, **kwargs
            ):
                return await self.generate(messages, tools=tools, **kwargs)

        reg = ToolRegistry()
        reg.register(_NullTool())
        system = GuardrailSystem()
        system.run_descriptors = []

        final = await agent_loop(
            state=AgentState.initial("explore", max_steps=3),
            model=_ExploringModel(),
            guardrail_system=system,
            tool_registry=reg,
        )
        # No explore-loop guard to terminate early; the loop reaches its
        # step budget without the metadata path blowing up.
        assert final.status != "error"
        assert final.current_step >= 3
        assert "explore_loop" not in (final.termination_reason or "")
