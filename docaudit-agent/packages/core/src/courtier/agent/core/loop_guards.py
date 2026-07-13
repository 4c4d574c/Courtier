"""Loop termination guards — detect and prevent futile agent behavior."""

from __future__ import annotations

import logging
from typing import Any

from courtier.config import get_settings

from .state import AgentState
from ..artifacts.resolver import emit_event

logger = logging.getLogger(__name__)

_EXPLORATORY_TOOLS = frozenset({"get_artifact", "list_artifacts"})


def check_explore_loop(
    state: AgentState,
    null_results: list[bool],
    tool_calls_history: list[tuple[str, str]],
    consecutive_exploratory: int = 0,
) -> bool:
    """Detect futile exploration patterns and return True if terminal.

    Three patterns are caught:
    1. Consecutive null/empty tool results.
    2. Repeated calls to the same tool with identical arguments.
    3. Too many consecutive read-only/exploratory tool calls.
    """
    if state.is_terminal():
        return False

    settings = get_settings()

    if len(null_results) >= settings.loop_max_null_tool_results and all(null_results[-settings.loop_max_null_tool_results:]):
        logger.warning(
            "Explore-loop detected: %d consecutive null results. Forcing completion.",
            settings.loop_max_null_tool_results,
        )
        emit_event("loop_no_progress_detected", {
            "reason": "null_results",
            "count": settings.loop_max_null_tool_results,
        })
        return True

    if len(tool_calls_history) >= settings.loop_max_same_tool_calls:
        recent = tool_calls_history[-settings.loop_max_same_tool_calls:]
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
            return True

    if consecutive_exploratory >= settings.loop_max_consecutive_exploratory:
        logger.warning(
            "Explore-loop detected: %d consecutive exploratory tool calls. Forcing completion.",
            consecutive_exploratory,
        )
        emit_event("loop_no_progress_detected", {
            "reason": "consecutive_exploratory",
            "count": consecutive_exploratory,
        })
        return True

    return False


def detect_reasoning_loop(recent_reasoning: list[str]) -> bool:
    """Check if the last N reasoning steps are near-duplicates."""
    from .loop_utils import similarity as _sim

    settings = get_settings()
    if len(recent_reasoning) < settings.loop_max_reasoning_dupe_steps:
        return False
    recent = recent_reasoning[-settings.loop_max_reasoning_dupe_steps:]
    return all(
        _sim(recent[i], recent[i + 1]) >= settings.loop_reasoning_similarity_threshold
        for i in range(len(recent) - 1)
    )


def check_business_artifact_progress(
    artifact_store: Any,
    initial_business_count: int,
    turns_since_last_business_artifact: int,
) -> tuple[int, int, bool]:
    """Track business artifact production.

    Returns (new_initial_count, new_turns, should_terminate).
    """
    settings = get_settings()

    if artifact_store is None:
        return initial_business_count, turns_since_last_business_artifact, False

    current_business = _count_business_artifacts(artifact_store)
    if current_business < 0:
        # Artifact store error — can't determine progress, skip this check
        return initial_business_count, turns_since_last_business_artifact, False
    if current_business <= initial_business_count:
        turns_since_last_business_artifact += 1
    else:
        turns_since_last_business_artifact = 0
        initial_business_count = current_business

    if turns_since_last_business_artifact >= settings.loop_max_turns_without_business_artifacts:
        logger.warning(
            "No-progress detected: %d turns without new business artifacts. Forcing completion.",
            turns_since_last_business_artifact,
        )
        emit_event("loop_no_progress_detected", {
            "reason": "no_business_artifacts",
            "turns": turns_since_last_business_artifact,
        })
        return initial_business_count, turns_since_last_business_artifact, True

    return initial_business_count, turns_since_last_business_artifact, False


def update_null_tracking(results: list, null_results: list[bool]) -> None:
    """Track whether results are null/empty for explore-loop detection."""
    settings = get_settings()
    for r in results:
        is_null = (
            r.raw_data is None
            or (isinstance(r.raw_data, dict) and not r.raw_data)
            or (isinstance(r.raw_data, list) and not r.raw_data)
        )
        null_results.append(is_null)
    while len(null_results) > settings.loop_max_null_tool_results * 2:
        null_results.pop(0)


def update_tool_call_history(
    tool_calls: tuple, history: list[tuple[str, str]]
) -> None:
    """Track tool call (name, args_key) for repeated-call detection."""
    from .loop_utils import normalize_args_for_dedup

    settings = get_settings()
    for tc in tool_calls:
        args_key = normalize_args_for_dedup(dict(tc.arguments))
        history.append((tc.name, args_key))
    while len(history) > settings.loop_max_same_tool_calls * 3:
        history.pop(0)


def update_exploratory_tracking(
    tool_calls: tuple, consecutive_exploratory: int,
) -> int:
    """Track consecutive exploratory (read-only) tool calls.

    Resets to 0 when a substantive tool call is made.
    """
    if not tool_calls:
        return consecutive_exploratory
    all_exploratory = all(tc.name in _EXPLORATORY_TOOLS for tc in tool_calls)
    return consecutive_exploratory + 1 if all_exploratory else 0


def _count_business_artifacts(artifact_store: Any) -> int:
    """Count non-debug artifacts in the store."""
    if artifact_store is None:
        return 0
    try:
        return len([
            a for a in artifact_store.list_all()
            if not a.metadata.debug_only
        ])
    except Exception:
        logger.warning("Failed to count active non-debug artifacts", exc_info=True)
        return -1  # Sentinel: caller should distinguish "error" from "truly zero"
