"""ExecutionResult — unified result abstraction for tools and agents.

This module lives in ``core`` so that both the agent state machinery and the
runtime package can import it without creating an import cycle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class ExecutionResult:
    """A unified result returned by both tools and sub-agents.

    Small results can be returned inline via ``raw_data``.  Medium results
    provide a ``summary`` plus ``key_excerpts``.  Large results are persisted
    externally and identified by ``result_id``.
    """

    success: bool
    actor_type: Literal["tool", "agent", "skill"]
    actor_name: str
    result_id: str | None = None
    raw_data: Any = None
    summary: str | None = None
    key_excerpts: tuple[str, ...] = ()
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    size_bytes: int = 0
    content_type: str = "application/json"

    def is_persisted(self) -> bool:
        """True when the full result is stored externally."""
        return self.result_id is not None

    def to_observation_dict(self) -> dict[str, Any]:
        """Serialize to the dict stored in a ``role='tool'`` observation.

        ``raw_data`` is the single canonical payload key — all consumers read
        it directly; there is no ``data`` alias.
        """
        return {
            "success": self.success,
            "actor_type": self.actor_type,
            "actor_name": self.actor_name,
            "result_id": self.result_id,
            "raw_data": self.raw_data,
            "summary": self.summary,
            "key_excerpts": list(self.key_excerpts),
            "error": self.error,
            "metadata": self.metadata,
            "size_bytes": self.size_bytes,
            "content_type": self.content_type,
        }

    @classmethod
    def from_error(
        cls,
        *,
        actor_type: Literal["tool", "agent", "skill"],
        actor_name: str,
        error: str,
        metadata: dict[str, Any] | None = None,
    ) -> "ExecutionResult":
        """Factory for a failed result."""
        return cls(
            success=False,
            actor_type=actor_type,
            actor_name=actor_name,
            error=error,
            metadata=metadata or {},
        )
