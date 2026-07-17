"""GuardrailSystem — orchestrate layered guardrail checks."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Literal

from ...telemetry.metrics import record_guardrail_blocked
from .base import GuardContext, GuardLayer, Guardrail, GuardResult

logger = logging.getLogger(__name__)

GuardMode = Literal["allow", "log", "block", "off"]


@dataclass
class GuardrailSystem:
    """Run a set of guardrails with per-layer default modes.

    Each layer can be configured as:
      - "off": skip the layer entirely
      - "log": run checks and emit log events but never block
      - "block": honor block actions (default)
      - "allow": skip checks and always allow
    """

    guardrails: list[Guardrail] = field(default_factory=list)
    input_mode: GuardMode = "block"
    output_mode: GuardMode = "log"
    tool_mode: GuardMode = "block"
    post_tool_mode: GuardMode = "block"
    on_event: Callable[[GuardResult], Awaitable[None]] | None = None

    def register(self, guardrail: Guardrail) -> None:
        """Add a guardrail to the system."""
        self.guardrails.append(guardrail)

    async def check(
        self,
        layer: GuardLayer,
        context: GuardContext,
    ) -> GuardResult:
        """Run all guardrails for *layer* and return the most restrictive result."""
        mode = self._mode_for_layer(layer)
        if mode == "off" or mode == "allow":
            return GuardResult.allow("guardrail_system")

        worst: GuardResult = GuardResult.allow("guardrail_system")
        for guard in self.guardrails:
            if guard.layer != layer:
                continue
            try:
                result = await guard.check(context)
            except Exception:
                logger.exception("Guardrail %s failed", guard.name)
                continue

            enriched = GuardResult(
                action=result.action,
                reason=result.reason,
                layer=layer,
                guard_name=guard.name,
                metadata=result.metadata,
            )

            if self.on_event is not None:
                try:
                    await self.on_event(enriched)
                except Exception:
                    logger.exception("Guardrail event handler failed")

            if mode == "log" and enriched.action == "block":
                # Downgrade block to log when layer is in log mode.
                enriched = GuardResult(
                    action="log",
                    reason=enriched.reason,
                    layer=layer,
                    guard_name=enriched.guard_name,
                    metadata={**enriched.metadata, "downgraded_from": "block"},
                )

            if _is_more_restrictive(enriched.action, worst.action):
                worst = enriched

            if worst.action == "block":
                record_guardrail_blocked(layer=layer, guard_name=enriched.guard_name)
                if mode == "block":
                    break

        return worst

    def _mode_for_layer(self, layer: GuardLayer) -> GuardMode:
        return {
            "input": self.input_mode,
            "output": self.output_mode,
            "tool": self.tool_mode,
            "post_tool": self.post_tool_mode,
        }.get(layer, "block")


def _is_more_restrictive(a: str, b: str) -> bool:
    """Return True if *a* is more restrictive than *b*."""
    order = {"allow": 0, "log": 1, "block": 2}
    return order.get(a, 0) > order.get(b, 0)
