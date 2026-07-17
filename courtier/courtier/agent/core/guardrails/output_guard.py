"""Output-layer guardrails."""

from __future__ import annotations

from .base import GuardContext, GuardLayer, GuardResult


class EmptyOutputGuard:
    """Detect empty or unhelpful LLM outputs."""

    name = "empty_output"
    layer: GuardLayer = "output"

    async def check(self, context: GuardContext) -> GuardResult:
        text = context.response_text
        if text is None:
            return GuardResult.allow(self.name)
        stripped = text.strip()
        if len(stripped) < 5:
            return GuardResult.log(
                self.name,
                f"Output is too short ({len(stripped)} chars)",
            )
        return GuardResult.allow(self.name)


class RefusalOutputGuard:
    """Detect common refusal patterns."""

    name = "refusal_output"
    layer: GuardLayer = "output"

    REFUSAL_PHRASES = [
        "i'm sorry",
        "i cannot",
        "i can't",
        "i'm not able",
    ]

    async def check(self, context: GuardContext) -> GuardResult:
        text = (context.response_text or "").lower()
        for phrase in self.REFUSAL_PHRASES:
            if phrase in text:
                return GuardResult.log(
                    self.name,
                    f"Possible refusal detected: {phrase!r}",
                )
        return GuardResult.allow(self.name)
