"""GuardrailSystem — unified run pipeline: guards, interceptors, observers.

One dispatch object per scope (see ``SCOPES``), three handler vocabularies
with fixed per-scope order **adapt → enforce → record**:

- guards (enforcement): ``check`` per layer / ``check_call`` per call,
  with per-layer modes and the dual failure contract documented in base.
- interceptors (adaptation): receive the scope's ``GuardContext`` and may
  return a new ``AgentState`` — always fail-open (a crashing interceptor
  is skipped and the run continues).
- observers (recording): fire-and-forget, exceptions always swallowed.

Merged from the former HookChain (pre_think / post_observe events): its
interceptors and observers are the same vocabulary here, dispatched at the
same lifecycle points with a defined order relative to guards.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Literal

from courtier.prompts.errors import render_error

from ...telemetry.metrics import record_guardrail_blocked
from .base import CallGuardResult, GuardContext, GuardLayer, Guardrail, GuardResult

if TYPE_CHECKING:
    from .registry import GuardDescriptor

logger = logging.getLogger(__name__)

GuardMode = Literal["allow", "log", "block", "off"]

#: Pipeline scopes and the guard layer enforced at each one.
SCOPES: dict[str, GuardLayer] = {
    "pre_think": "input",
    "output": "output",
    "tool": "tool",
    "tool_call": "tool_call",
    "post_tool": "post_tool",
}

#: Interceptor: receives the scope context, returns a new AgentState or None.
InterceptorHandler = Callable[[GuardContext], Awaitable[Any]]
#: Observer: receives the scope context, returns nothing.
ObserverHandler = Callable[[GuardContext], Awaitable[None]]


@dataclass
class ScopeOutcome:
    """Result of one ``run_scope`` dispatch."""

    #: (Possibly interceptor-updated) state.
    state: Any
    #: Set when the scope's guard layer blocked (block mode only).
    blocked: GuardResult | None = None
    #: Aggregate guard result of the scope's layer (allow when none ran);
    #: carries guard metadata such as ExploreLoopGuard's counters.
    guard_result: GuardResult = field(default_factory=lambda: GuardResult.allow("guardrail_system"))


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
    #: Run-scoped guard declarations (settings ``guardrail_guards`` entries
    #: with ``scope: run``). Recipes, not instances: ``agent_loop``
    #: instantiates fresh guards from them at the start of every loop —
    #: orchestrator turn and each sub-agent turn alike — and unregisters
    #: them in its finally, so per-run state never mixes across the agents
    #: sharing this system.
    run_descriptors: list["GuardDescriptor"] = field(default_factory=list)
    input_mode: GuardMode = "block"
    output_mode: GuardMode = "log"
    tool_mode: GuardMode = "block"
    tool_call_mode: GuardMode = "block"
    post_tool_mode: GuardMode = "block"
    on_event: Callable[[GuardResult], Awaitable[None]] | None = None
    #: Default interceptor timeout in seconds (None = no timeout).
    interceptor_timeout: float | None = None
    _interceptors: dict[str, list[tuple[int, int, InterceptorHandler, float | None]]] = field(
        default_factory=lambda: defaultdict(list)
    )
    _observers: dict[str, list[ObserverHandler]] = field(default_factory=lambda: defaultdict(list))
    _registration_counter: int = 0
    _agent_name: str = ""
    _session_id: str = ""
    _context_metadata: dict[str, Any] = field(default_factory=dict)

    def register(self, guardrail: Guardrail) -> None:
        """Add a guardrail to the system.

        Stateful per-run guards are registered as fresh instances and
        removed again via :meth:`unregister` (see ``agent_loop``).
        """
        self.guardrails.append(guardrail)

    def unregister(self, guardrail: Guardrail) -> None:
        """Remove a previously registered guardrail (idempotent)."""
        self.guardrails = [g for g in self.guardrails if g is not guardrail]

    # ------------------------------------------------------------------
    # Pipeline: interceptors / observers / session context
    # ------------------------------------------------------------------

    def register_interceptor(
        self,
        scope: str,
        handler: InterceptorHandler,
        *,
        priority: int = 0,
        timeout: float | None = None,
    ) -> None:
        """Register a state-adapting interceptor at a pipeline scope.

        Interceptors run before the scope's guards (higher priority first,
        stable by registration order), always fail-open, and may return a
        new AgentState (or None to leave it unchanged). The ``tool_call``
        scope is decision-only — adaptation there would let an extension
        rewrite or inject calls, so registering raises.
        """
        if scope == "tool_call":
            raise ValueError("tool_call scope is decision-only: interceptors are not allowed")
        if scope not in SCOPES:
            raise ValueError(f"unknown pipeline scope: {scope!r}")
        self._interceptors[scope].append((priority, self._registration_counter, handler, timeout))
        self._registration_counter += 1

    def register_observer(self, scope: str, handler: ObserverHandler) -> None:
        """Register a fire-and-forget observer at a pipeline scope."""
        if scope not in SCOPES:
            raise ValueError(f"unknown pipeline scope: {scope!r}")
        self._observers[scope].append(handler)

    def set_context(
        self, *, agent_name: str = "", session_id: str = "", **metadata: Any
    ) -> dict[str, Any]:
        """Set session-level context injected into every scope dispatch.

        Returns the previous context so a run can restore it when it ends —
        sub-agent loops share the session system and would otherwise leave
        their identity behind (orchestrator scopes after a nested run would
        misattribute).
        """
        previous = {
            "agent_name": self._agent_name,
            "session_id": self._session_id,
            "metadata": dict(self._context_metadata),
        }
        self._agent_name = agent_name
        self._session_id = session_id
        self._context_metadata = dict(metadata)
        return previous

    def _enrich_context(self, context: GuardContext) -> None:
        """Inject the stored session identity into one dispatch context.

        Empty stored values never override what the caller already set.
        """
        if self._agent_name:
            context.agent_name = self._agent_name
        if self._session_id:
            context.session_id = self._session_id
        if self._context_metadata:
            context.metadata = {**self._context_metadata, **context.metadata}

    async def run_scope(self, scope: str, context: GuardContext) -> ScopeOutcome:
        """Dispatch one pipeline scope: adapt → enforce → record.

        - interceptors (adapt, fail-open): may return a new AgentState;
          the context's state is kept in sync for downstream handlers.
        - guards (enforce): the scope's guard layer via ``check`` with its
          per-layer mode. ``tool_call`` is dispatched per call by the loop
          (``check_call``) and never here.
        - observers (record): exceptions always swallowed.
        """
        state = context.state
        self._enrich_context(context)

        entries = sorted(self._interceptors.get(scope, []), key=lambda e: (-e[0], e[1]))
        for _priority, _order, handler, per_timeout in entries:
            timeout = per_timeout if per_timeout is not None else self.interceptor_timeout
            try:
                if timeout is not None:
                    result = await asyncio.wait_for(handler(context), timeout=timeout)
                else:
                    result = await handler(context)
            except Exception:
                logger.exception("Interceptor failed (fail-open): scope=%s", scope)
                continue
            if result is not None:
                state = result
                context.state = state

        blocked: GuardResult | None = None
        aggregate = GuardResult.allow("guardrail_system")
        if scope != "tool_call":
            mode = self._mode_for_layer(SCOPES[scope])
            if mode not in ("off", "allow"):
                aggregate = await self.check(SCOPES[scope], context)
                if aggregate.action == "block" and mode == "block":
                    blocked = aggregate

        for observer in self._observers.get(scope, []):
            try:
                await observer(context)
            except Exception:
                logger.exception("Observer failed: scope=%s", scope)

        return ScopeOutcome(state=state, blocked=blocked, guard_result=aggregate)

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
        # Guard metadata is additive: every executed result's metadata is
        # merged (execution order, later keys win) into the aggregate.
        # Strictness only decides the aggregate action/reason/guard_name —
        # an allow-with-metadata must not be swallowed by the initial blank
        # allow, or guards' side-channel data (e.g. ExploreLoopGuard's
        # consecutive_exploratory counter) silently disappears.
        merged_metadata: dict[str, Any] = {}
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
            merged_metadata.update(enriched.metadata)

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

        if merged_metadata:
            worst = GuardResult(
                action=worst.action,
                reason=worst.reason,
                layer=worst.layer,
                guard_name=worst.guard_name,
                metadata=merged_metadata,
            )
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

        # The loop passes a bare context here — inject the session identity
        # the same way run_scope does for the turn-level scopes.
        self._enrich_context(context)

        confirm_result: CallGuardResult | None = None
        for guard in self.guardrails:
            check_call = getattr(guard, "check_call", None)
            if check_call is None or guard.layer != layer:
                continue
            try:
                result = await check_call(call, context)
            except Exception:
                if mode == "log":
                    logger.exception("Call guardrail %s failed (shadow mode)", guard.name)
                    continue
                logger.exception("Call guardrail %s failed — failing closed", guard.name)
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
            metadata=(
                dict(result.metadata, error_code=result.error_code)
                if result.error_code
                else dict(result.metadata)
            ),
        )
        try:
            await self.on_event(mirrored)
        except Exception:
            logger.exception("Guardrail event handler failed")


def _is_more_restrictive(a: str, b: str) -> bool:
    """Return True if *a* is more restrictive than *b*."""
    order = {"allow": 0, "log": 1, "block": 2}
    return order.get(a, 0) > order.get(b, 0)
