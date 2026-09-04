"""Domain-contributed guardrails: thin wrapper over the shared registry.

A domain package may declare in-process guardrails in its domain.yaml
(``guards: ["<module>.<Class>", ...]``). Guards are loaded at activation
time and registered into the session GuardrailSystem; the owning domain is
recorded in the registration log line only. The domain channel keeps the
no-arg construction contract (a session context is not threaded through
activation); see ``registry`` for the general mechanism.

Guards never touch the network or the filesystem at check time — guards
participating in the ``tool_call`` layer must be pure in-memory decisions
(see guardrails/base module docstring).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from .guardrail_system import GuardrailSystem
from .registry import (
    REQUIRED_GUARD_ATTRIBUTES,
    GuardLoadError,
    instantiate_guard,
)

logger = logging.getLogger(__name__)

__all__ = [
    "REQUIRED_GUARD_ATTRIBUTES",
    "GuardLoadError",
    "load_domain_guard",
    "register_domain_guards",
]


def load_domain_guard(class_path: str):
    """Import and instantiate a declared domain guard (no-arg constructor)."""
    return instantiate_guard(class_path)


def register_domain_guards(
    system: GuardrailSystem,
    class_paths: Sequence[str],
    *,
    owner: str,
) -> list[str]:
    """Load and register each declared guard; *owner* names the domain in logs.

    A failing declaration is logged and skipped — a broken domain guard
    must not fail the domain's activation. Returns registered guard names.
    """
    registered: list[str] = []
    for class_path in class_paths:
        try:
            guard = load_domain_guard(class_path)
        except GuardLoadError:
            logger.warning("Skipping domain guard: %s", class_path, exc_info=True)
            continue
        system.register(guard)
        registered.append(getattr(guard, "name"))
    if registered:
        logger.info("Registered domain guards %s (owner=%s)", registered, owner)
    return registered
