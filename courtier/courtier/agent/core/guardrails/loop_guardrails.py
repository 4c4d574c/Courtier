"""Guardrail implementations migrated from loop_guards.py.

These guards previously lived as standalone functions in
``courtier.agent.core.loop_guards`` and were called directly from
``agent_loop``. They are now stateful ``Guardrail`` implementations that
maintain their own per-run history and are invoked through ``GuardrailSystem``.
"""

from __future__ import annotations

import logging
from typing import Any

from courtier.config import get_settings

from ...artifacts.resolver import emit_event
from ..loop_utils import normalize_args_for_dedup
from ..tool_call import ToolCall
from .base import GuardContext, GuardLayer, GuardResult

logger = logging.getLogger(__name__)

_EXPLORATORY_TOOLS = frozenset({"get_artifact", "list_artifacts"})


class ExploreLoopGuard:
    """Detect futile exploration patterns in tool calls."""

    name = "explore_loop"
    layer: GuardLayer = "post_tool"

    def __init__(self) -> None:
        self._null_results: list[bool] = []
        self._tool_history: list[tuple[str, str]] = []
        self._consecutive_exploratory: int = 0

    @property
    def consecutive_exploratory(self) -> int:
        """Expose the current consecutive exploratory count for other loop logic."""
        return self._consecutive_exploratory

    async def check(self, context: GuardContext) -> GuardResult:
        settings = get_settings()
        tool_calls = context.tool_calls or ()
        tool_results = context.tool_results or ()

        self._update_tracking(tool_calls, tool_results)

        if len(self._null_results) >= settings.loop_max_null_tool_results and all(
            self._null_results[-settings.loop_max_null_tool_results :]
        ):
            logger.warning(
                "Explore-loop detected: %d consecutive null results. Forcing completion.",
                settings.loop_max_null_tool_results,
            )
            emit_event("loop_no_progress_detected", {
                "reason": "null_results",
                "count": settings.loop_max_null_tool_results,
            })
            return GuardResult.block(
                self.name,
                f"{settings.loop_max_null_tool_results} consecutive null tool results",
            )

        if len(self._tool_history) >= settings.loop_max_same_tool_calls:
            recent = self._tool_history[-settings.loop_max_same_tool_calls :]
            if len(set(recent)) == 1:
                logger.warning(
                    "Explore-loop detected: %d repeated calls to %s. Forcing completion.",
                    settings.loop_max_same_tool_calls,
                    recent[0],
                )
                emit_event("loop_no_progress_detected", {
                    "reason": "repeated_tool_call",
                    "tool": recent[0][0],
                    "count": settings.loop_max_same_tool_calls,
                })
                return GuardResult.block(
                    self.name,
                    f"{settings.loop_max_same_tool_calls} repeated calls to {recent[0][0]}",
                )

        if self._consecutive_exploratory >= settings.loop_max_consecutive_exploratory:
            logger.warning(
                "Explore-loop detected: %d consecutive exploratory tool calls. Forcing completion.",
                self._consecutive_exploratory,
            )
            emit_event("loop_no_progress_detected", {
                "reason": "consecutive_exploratory",
                "count": self._consecutive_exploratory,
            })
            return GuardResult.block(
                self.name,
                f"{self._consecutive_exploratory} consecutive exploratory tool calls",
            )

        return GuardResult.allow(
            self.name,
            metadata={"consecutive_exploratory": self._consecutive_exploratory},
        )

    def _update_tracking(
        self,
        tool_calls: Any,
        tool_results: Any,
    ) -> None:
        settings = get_settings()
        for r in tool_results:
            raw = getattr(r, "raw_data", r)
            is_null = (
                raw is None
                or (isinstance(raw, dict) and not raw)
                or (isinstance(raw, list) and not raw)
            )
            self._null_results.append(is_null)
        while len(self._null_results) > settings.loop_max_null_tool_results * 2:
            self._null_results.pop(0)

        for tc in tool_calls:
            name = tc.name if isinstance(tc, ToolCall) else tc.get("name", "")
            args = dict(tc.arguments) if isinstance(tc, ToolCall) else tc.get("arguments", {})
            args_key = normalize_args_for_dedup(args)
            self._tool_history.append((name, args_key))
        while len(self._tool_history) > settings.loop_max_same_tool_calls * 3:
            self._tool_history.pop(0)

        if tool_calls:
            all_exploratory = all(
                (tc.name if isinstance(tc, ToolCall) else tc.get("name", "")) in _EXPLORATORY_TOOLS
                for tc in tool_calls
            )
            self._consecutive_exploratory = (
                self._consecutive_exploratory + 1 if all_exploratory else 0
            )


class BusinessArtifactProgressGuard:
    """Detect turns without new business (non-debug) artifacts."""

    name = "business_artifact_progress"
    layer: GuardLayer = "post_tool"

    def __init__(self) -> None:
        self._initial_count: int | None = None
        self._turns_since_progress: int = 0

    async def check(self, context: GuardContext) -> GuardResult:
        artifact_store = context.metadata.get("artifact_store")
        if artifact_store is None:
            return GuardResult.allow(self.name)

        settings = get_settings()
        current_count = self._count_business_artifacts(artifact_store)
        if current_count < 0:
            return GuardResult.allow(self.name)

        if self._initial_count is None:
            self._initial_count = current_count

        if current_count <= self._initial_count:
            self._turns_since_progress += 1
        else:
            self._turns_since_progress = 0
            self._initial_count = current_count

        if self._turns_since_progress >= settings.loop_max_turns_without_business_artifacts:
            logger.warning(
                "No-progress detected: %d turns without new business artifacts. "
                "Forcing completion.",
                self._turns_since_progress,
            )
            emit_event("loop_no_progress_detected", {
                "reason": "no_business_artifacts",
                "turns": self._turns_since_progress,
            })
            return GuardResult.block(
                self.name,
                f"{self._turns_since_progress} turns without new business artifacts",
            )

        return GuardResult.allow(self.name)

    @staticmethod
    def _count_business_artifacts(artifact_store: Any) -> int:
        try:
            return len([
                a for a in artifact_store.list_all()
                if not a.metadata.debug_only
            ])
        except Exception:
            logger.warning("Failed to count active non-debug artifacts", exc_info=True)
            return -1
