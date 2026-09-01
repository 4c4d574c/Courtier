"""Tests for AgentRuntimeBudget."""

import pytest

from courtier.agent.runtime.budget import AgentRuntimeBudget


def test_default_budget_allows_spawn():
    budget = AgentRuntimeBudget()
    allowed, reason = budget.can_spawn("format_audit")
    assert allowed is True
    assert reason is None


def test_depth_limit_blocks_spawn():
    budget = AgentRuntimeBudget(max_depth=1)
    child = budget.allocate_child("a", "h1")
    allowed, reason = child.can_spawn("b")
    assert allowed is False
    assert "max_depth_reached" in reason


def test_total_spawn_limit_blocks_spawn():
    budget = AgentRuntimeBudget(remaining_total_spawns=1)
    child = budget.allocate_child("a", "h1")
    allowed, reason = child.can_spawn("b")
    assert allowed is False
    assert "total_spawn_budget_exhausted" in reason


def test_cycle_detection_blocks_spawn():
    budget = AgentRuntimeBudget()
    child = budget.allocate_child("a", "h1")
    allowed, reason = child.can_spawn("a")
    assert allowed is False
    assert "spawn_cycle_detected" in reason


def test_child_budget_increments_depth_and_chain():
    budget = AgentRuntimeBudget()
    child = budget.allocate_child("a", "h1")
    assert child.remaining_total_spawns == budget.remaining_total_spawns - 1
    assert child.parent_chain == ("h1",)
    assert child.agent_chain == ("a",)


def test_allocate_child_preserves_runtime_and_turns():
    budget = AgentRuntimeBudget(max_runtime_seconds=100, max_turns=10)
    child = budget.allocate_child("a", "h1")
    assert child.max_runtime_seconds == 100
    assert child.max_turns == 10


def test_allocate_child_allows_overrides():
    budget = AgentRuntimeBudget(max_runtime_seconds=100)
    child = budget.allocate_child("a", "h1", max_runtime_seconds=50, max_turns=5)
    assert child.max_runtime_seconds == 50
    assert child.max_turns == 5


def test_allocate_child_raises_when_not_allowed():
    budget = AgentRuntimeBudget(max_depth=0)
    with pytest.raises(RuntimeError, match="嵌套深度"):
        budget.allocate_child("a", "h1")
