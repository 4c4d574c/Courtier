"""Refusal detection — model-behavior recovery policy, not a guardrail.

The detector is a plain substring matcher over the configured pattern list
(``refusal_patterns`` setting, Chinese + English defaults). It is consulted
by the think phase for pure text responses (no tool calls); a hit triggers
the same-model retry chain (docs/architecture/refusal-retry-plan.md). It
never blocks anything: the refusal handling lives in the think-phase
policy, deliberately outside the guardrails pipeline.
"""

from __future__ import annotations

from collections.abc import Iterable


class RefusalDetector:
    """Case-insensitive substring matcher over the configured patterns."""

    def __init__(self, patterns: Iterable[str]) -> None:
        self._patterns = [p.lower() for p in patterns if p and p.strip()]

    def match(self, text: str | None) -> str | None:
        """Return the matched fragment (original casing) or None."""
        if not text:
            return None
        lowered = text.lower()
        for pattern in self._patterns:
            idx = lowered.find(pattern)
            if idx != -1:
                return text[idx : idx + len(pattern)]
        return None
