"""Reasoning-loop detection for the agent loop.

The explore-loop / business-artifact guards were migrated to
``courtier.agent.core.guardrails.loop_guardrails`` (driven by
``GuardrailSystem``); this module keeps only ``detect_reasoning_loop``,
which is used by the think phase.
"""

from __future__ import annotations

import logging

from courtier.config import get_settings

logger = logging.getLogger(__name__)


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
