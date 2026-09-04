"""Test-only factory-built guard for the declarative registry tests.

Lives under ``courtier.agent.testing`` so tests can reference it by dotted
path exactly like a real ``guardrail_guards`` / domain.yaml declaration.
"""

from __future__ import annotations

from courtier.agent.core.guardrails import GuardResult
from courtier.agent.core.guardrails.registry import GuardSessionContext


class FactoryBuiltGuard:
    """Input-layer guard built via ``build(ctx)`` — records the workspace root."""

    name = "factory_built_guard"
    layer = "input"

    def __init__(self) -> None:
        self.workspace: str | None = None

    @classmethod
    def build(cls, ctx: GuardSessionContext) -> "FactoryBuiltGuard":
        guard = cls()
        guard.workspace = str(ctx.session_workspace)
        return guard

    async def check(self, context) -> GuardResult:
        return GuardResult.allow(self.name)


class RequiresArgsGuard:
    """Guard whose constructor demands an argument and has no build factory."""

    name = "requires_args_guard"
    layer = "post_tool"

    def __init__(self, threshold: int) -> None:  # noqa: ARG002 — shape-test fixture
        self._threshold = threshold


class StatefulRunGuard:
    """Run-scoped stateful guard fixture for the per-run lifecycle contract.

    Records the ``id()`` of every instance ever built in ``instances`` so
    tests can assert that each ``agent_loop`` materializes a fresh guard and
    that state never survives across runs."""

    name = "stateful_run"
    layer = "post_tool"

    instances: list[int] = []

    def __init__(self) -> None:
        self.checks = 0
        type(self).instances.append(id(self))

    async def check(self, context) -> GuardResult:
        self.checks += 1
        return GuardResult.allow(self.name, metadata={"checks": self.checks})
