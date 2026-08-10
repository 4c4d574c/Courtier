"""AgentHandle — reference to a spawned sub-agent instance."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from .budget import AgentRuntimeBudget


@dataclass(frozen=True)
class AgentHandle:
    """Immutable reference to a spawned agent.

    Holds everything the runtime needs to delegate a task to the agent and
    to stream its events back to the parent.
    """

    handle_id: str
    agent_name: str
    agent_type: str
    task: str
    budget: AgentRuntimeBudget
    parent_handle_id: str | None = None
    parent_subagent_name: str | None = None
    context_mode: str = "blackbox"
    context: dict[str, str] | None = None
    ref_ids: list[str] = field(default_factory=list)
    model_config: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    scope_id: str | None = None

    @classmethod
    def create(
        cls,
        *,
        agent_name: str,
        agent_type: str,
        task: str,
        budget: AgentRuntimeBudget,
        handle_id: str | None = None,
        parent_handle_id: str | None = None,
        parent_subagent_name: str | None = None,
        context_mode: str = "blackbox",
        context: dict[str, str] | None = None,
        ref_ids: list[str] | None = None,
        model_config: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        scope_id: str | None = None,
    ) -> "AgentHandle":
        """Factory that generates a fresh UUID handle id."""
        return cls(
            handle_id=handle_id or f"h-{uuid4().hex[:16]}",
            agent_name=agent_name,
            agent_type=agent_type,
            task=task,
            budget=budget,
            parent_handle_id=parent_handle_id,
            parent_subagent_name=parent_subagent_name,
            context_mode=context_mode,
            context=context,
            ref_ids=ref_ids or [],
            model_config=model_config or {},
            metadata=metadata or {},
            scope_id=scope_id,
        )

    def child_handle(
        self,
        *,
        agent_name: str,
        agent_type: str,
        task: str,
        budget: AgentRuntimeBudget,
        context_mode: str = "blackbox",
        context: dict[str, str] | None = None,
        ref_ids: list[str] | None = None,
        model_config: dict[str, Any] | None = None,
        scope_id: str | None = None,
    ) -> "AgentHandle":
        """Create a child handle reusing this handle's runtime context."""
        return AgentHandle.create(
            agent_name=agent_name,
            agent_type=agent_type,
            task=task,
            budget=budget,
            parent_handle_id=self.handle_id,
            parent_subagent_name=self.agent_name,
            context_mode=context_mode,
            context=context if context is not None else self.context,
            ref_ids=ref_ids or list(self.ref_ids),
            model_config=model_config or dict(self.model_config),
            metadata={"parent_agent_type": self.agent_type},
            scope_id=scope_id or self.scope_id,
        )
