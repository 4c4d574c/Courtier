"""GuardrailSystem — orchestrate layered guardrail checks."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Literal

from courtier.prompts.errors import render_error

from ...telemetry.metrics import record_guardrail_blocked
from .base import CallGuardResult, GuardContext, GuardLayer, Guardrail, GuardResult

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
    tool_call_mode: GuardMode = "block"
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
            "tool_call": self.tool_call_mode,
            "post_tool": self.post_tool_mode,
        }.get(layer, "block")

    async def check_call(
        self,
        layer: GuardLayer,
        call: Any,
        context: GuardContext,
    ) -> CallGuardResult:
        """Run all per-call guards for *layer* against a single tool call.

        Enforcement layer semantics — deliberately fail-closed, unlike
        ``check`` (see base module docstring):

        - block mode: first ``deny`` wins; a guard raising is itself a
          denial (``error_code: permission_error``).
        - log mode: shadow — results are recorded via ``on_event`` and the
          metric is untouched; nothing is enforced (returns allow).
        - off/allow: skip entirely.

        Guards without a ``check_call`` method do not participate, whatever
        their declared layer. ``confirm`` results pass through to the caller
        (no in-system consumer yet).
        """
        mode = self._mode_for_layer(layer)
        if mode == "off" or mode == "allow":
            return CallGuardResult.allow("guardrail_system")

        confirm_result: CallGuardResult | None = None
        for guard in self.guardrails:
            check_call = getattr(guard, "check_call", None)
            if check_call is None or guard.layer != layer:
                continue
            try:
                result = await check_call(call, context)
            except Exception:
                if mode == "log":
                    logger.exception(
                        "Call guardrail %s failed (shadow mode)", guard.name
                    )
                    continue
                logger.exception(
                    "Call guardrail %s failed — failing closed", guard.name
                )
                denied = CallGuardResult.deny(
                    guard.name,
                    render_error("errors.guard_call_failed", guard_name=guard.name),
                    error_code="permission_error",
                )
                await self._emit_call_result(layer, denied)
                record_guardrail_blocked(layer=layer, guard_name=guard.name)
                return denied

            await self._emit_call_result(layer, result)

            if mode == "log":
                continue
            if result.action == "deny":
                record_guardrail_blocked(layer=layer, guard_name=guard.name)
                return result
            if result.action == "confirm" and confirm_result is None:
                confirm_result = result

        return confirm_result or CallGuardResult.allow("guardrail_system")

    async def _emit_call_result(self, layer: GuardLayer, result: CallGuardResult) -> None:
        """Mirror a per-call result into the GuardResult event channel.

        Action mapping keeps the ``guard.triggered`` vocabulary unchanged:
        deny→block (counts as a blocked call), confirm→log (observational).
        """
        if self.on_event is None:
            return
        mirrored = GuardResult(
            action={
                "allow": "allow",
                "deny": "block",
                "confirm": "log",
            }.get(result.action, "allow"),
            reason=result.reason,
            layer=layer,
            guard_name=result.guard_name,
            metadata=dict(result.metadata, error_code=result.error_code)
            if result.error_code
            else dict(result.metadata),
        )
        try:
            await self.on_event(mirrored)
        except Exception:
            logger.exception("Guardrail event handler failed")


def _is_more_restrictive(a: str, b: str) -> bool:
    """Return True if *a* is more restrictive than *b*."""
    order = {"allow": 0, "log": 1, "block": 2}
    return order.get(a, 0) > order.get(b, 0)
