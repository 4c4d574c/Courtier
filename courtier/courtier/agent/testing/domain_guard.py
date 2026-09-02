"""Test-only domain guard for the domain-guard mechanism tests.

Lives under ``courtier.agent.testing`` so tests can reference it by
dotted path exactly like a real domain.yaml ``guards:`` declaration.
"""

from __future__ import annotations

from courtier.agent.core.guardrails import CallGuardResult


class DummyDomainGuard:
    """Tool_call-layer guard that records seen tool names and allows all."""

    name = "dummy_domain_guard"
    layer = "tool_call"

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def check_call(self, call, context) -> CallGuardResult:
        self.calls.append(getattr(call, "name", "?"))
        return CallGuardResult.allow(self.name)


class BadLayerDomainGuard:
    """Test-only guard declaring an illegal layer value."""

    name = "bad_layer_domain_guard"
    layer = "bogus_layer"

    def __init__(self) -> None:
        pass
