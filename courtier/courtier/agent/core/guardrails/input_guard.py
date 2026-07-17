"""Input-layer guardrails."""

from __future__ import annotations

import re

from .base import GuardContext, GuardLayer, GuardResult


class SensitiveInputGuard:
    """Detect potentially sensitive patterns in user input."""

    name = "sensitive_input"
    layer: GuardLayer = "input"

    # Simple default patterns; callers can subclass or configure.
    DEFAULT_PATTERNS = [
        re.compile(r"\b\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?\d{4}\b"),  # credit-card-like
        re.compile(r"\b(?:password|passwd|pwd)\s*[:=]\s*\S+", re.IGNORECASE),
    ]

    def __init__(self, patterns: list[re.Pattern[str]] | None = None) -> None:
        self.patterns = patterns or list(self.DEFAULT_PATTERNS)

    async def check(self, context: GuardContext) -> GuardResult:
        messages = context.messages
        if messages is None:
            return GuardResult.allow(self.name)

        text = ""
        if isinstance(messages, (list, tuple)):
            for msg in messages:
                if isinstance(msg, dict):
                    content = msg.get("content", "")
                else:
                    content = getattr(msg, "content", msg)
                if isinstance(content, str):
                    text += content + "\n"
        elif isinstance(messages, str):
            text = messages

        for pattern in self.patterns:
            match = pattern.search(text)
            if match:
                return GuardResult.log(
                    self.name,
                    f"Potential sensitive pattern matched: {match.group()[:20]}...",
                )
        return GuardResult.allow(self.name)
