"""AgentRuntimeBudget — immutable resource budget for sub-agent trees."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AgentRuntimeBudget:
    """Resource budget allocated to an agent handle and its descendants.

    The budget is immutable; allocating a child budget returns a new instance.
    """

    max_depth: int = 5
    remaining_total_spawns: int = 20
    max_runtime_seconds: float = 300.0
    max_cumulative_runtime_seconds: float = 600.0
    max_turns: int = 20
    parent_chain: tuple[str, ...] = ()
    agent_chain: tuple[str, ...] = ()

    def can_spawn(self, agent_name: str) -> tuple[bool, str | None]:
        """Check whether a child agent can be spawned.

        Returns (allowed, reason).  ``reason`` is ``None`` when allowed.
        """
        if len(self.parent_chain) >= self.max_depth:
            return False, f"max_depth_reached ({self.max_depth})"
        if self.remaining_total_spawns <= 0:
            return False, "total_spawn_budget_exhausted"
        if agent_name in self.agent_chain:
            return False, f"spawn_cycle_detected for agent {agent_name!r}"
        return True, None

    def allocate_child(
        self,
        agent_name: str,
        handle_id: str,
        *,
        max_runtime_seconds: float | None = None,
        max_turns: int | None = None,
    ) -> "AgentRuntimeBudget":
        """Return a new budget for a child agent."""
        allowed, reason = self.can_spawn(agent_name)
        if not allowed:
            raise RuntimeError(f"Cannot allocate child budget: {reason}")

        return AgentRuntimeBudget(
            max_depth=self.max_depth,
            remaining_total_spawns=self.remaining_total_spawns - 1,
            max_runtime_seconds=max_runtime_seconds
            if max_runtime_seconds is not None
            else self.max_runtime_seconds,
            max_cumulative_runtime_seconds=self.max_cumulative_runtime_seconds,
            max_turns=max_turns if max_turns is not None else self.max_turns,
            parent_chain=self.parent_chain + (handle_id,),
            agent_chain=self.agent_chain + (agent_name,),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize for logging / metadata."""
        return {
            "max_depth": self.max_depth,
            "remaining_total_spawns": self.remaining_total_spawns,
            "max_runtime_seconds": self.max_runtime_seconds,
            "max_cumulative_runtime_seconds": self.max_cumulative_runtime_seconds,
            "max_turns": self.max_turns,
            "depth": len(self.parent_chain),
        }
