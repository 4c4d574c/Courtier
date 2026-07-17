"""Explicit state machine for the agent loop.

The state machine centralizes valid status transitions and records a
``transition_id`` on each state change. This makes agent execution auditable,
interruptible, and replayable while keeping ``agent_loop`` free of scattered
status assignments.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from ..telemetry.metrics import record_state_machine_invalid
from .event_bus import EventBus
from .events import AgentEvent
from .state import AgentState, AgentStatus

logger = logging.getLogger(__name__)


class InvalidStateTransition(Exception):
    """Raised when a requested transition is not allowed."""

    def __init__(self, from_status: AgentStatus, to_status: AgentStatus) -> None:
        self.from_status = from_status
        self.to_status = to_status
        super().__init__(f"Invalid transition from {from_status!r} to {to_status!r}")


@dataclass(frozen=True)
class Transition:
    """A single state transition record."""

    transition_id: str
    from_status: AgentStatus
    to_status: AgentStatus
    reason: str


class AgentStateMachine:
    """Encapsulates valid ``AgentState`` status transitions."""

    VALID_TRANSITIONS: dict[AgentStatus, set[AgentStatus]] = {
        # "blocked" is reachable from any active state: input-layer guardrails
        # fire before the think transition (idle/observing), and permission
        # denies fire while waiting_for_tool.  "error" is reachable from idle
        # for failures before the first think transition (e.g. hook crashes).
        "idle": {"thinking", "blocked", "error"},
        "thinking": {"waiting_for_tool", "completed", "error"},
        "waiting_for_tool": {"observing", "blocked", "error"},
        "observing": {"thinking", "completed", "error", "blocked"},
        "blocked": set(),
        "completed": set(),
        "error": set(),
    }

    def __init__(
        self,
        strict: bool = True,
        *,
        event_bus: EventBus | None = None,
        session_id: str = "",
        agent_name: str = "",
    ) -> None:
        self.strict = strict
        self._event_bus = event_bus
        self._session_id = session_id
        self._agent_name = agent_name

    def transition(
        self,
        state: AgentState,
        to_status: AgentStatus,
        reason: str = "",
    ) -> AgentState:
        """Return a new ``AgentState`` with *to_status* if the transition is valid.

        In non-strict mode invalid transitions are logged but allowed, which
        simplifies incremental adoption and debugging. The returned state carries
        a fresh ``transition_id`` for audit/replay.
        """
        from_status = state.status
        if from_status == to_status:
            # No-op transition: preserve existing state and reason.
            return state

        allowed = self.VALID_TRANSITIONS.get(from_status, set())
        if to_status not in allowed:
            if self.strict:
                raise InvalidStateTransition(from_status, to_status)
            # Non-strict: warn and proceed so callers can opt into enforcement
            # gradually.
            logger.warning(
                "Allowing invalid state transition %s -> %s (reason: %s)",
                from_status,
                to_status,
                reason,
            )
            record_state_machine_invalid(from_status, to_status)

        transition_id = uuid.uuid4().hex
        update: dict[str, Any] = {
            "status": to_status,
            "transition_id": transition_id,
        }
        if to_status in ("blocked", "error", "completed"):
            update["termination_reason"] = reason
        return state.model_copy(update=update)

    async def transition_async(
        self,
        state: AgentState,
        to_status: AgentStatus,
        reason: str = "",
    ) -> AgentState:
        """Like ``transition()`` but also publishes a ``state.transition`` event."""
        new_state = self.transition(state, to_status, reason)
        if self._event_bus is not None and new_state is not state:
            await self._event_bus.publish(
                AgentEvent(
                    type="state.transition",
                    session_id=self._session_id,
                    agent_name=self._agent_name,
                    turn_index=new_state.current_step,
                    payload={
                        "from": state.status,
                        "to": to_status,
                        "reason": reason,
                        "transition_id": new_state.transition_id,
                    },
                )
            )
        return new_state

    def is_terminal(self, status: AgentStatus) -> bool:
        """Check if *status* is a terminal state."""
        return status in ("completed", "blocked", "error")

    def can_transition(self, from_status: AgentStatus, to_status: AgentStatus) -> bool:
        """Return whether *from_status* -> *to_status* is defined."""
        return to_status in self.VALID_TRANSITIONS.get(from_status, set())
