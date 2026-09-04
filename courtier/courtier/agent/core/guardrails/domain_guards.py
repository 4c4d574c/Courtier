"""Domain-contributed guardrails: thin wrapper over the shared registry.

A domain package may declare in-process guardrails in its domain.yaml —
``guards:`` entries are dotted class paths (``"docaudit.guards.FormatGuard"``:
session-scoped, no-arg) or declaration objects (``{name, class_path, scope,
enabled}``) that may declare ``scope: run``. Guards are loaded at activation
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
from typing import Any

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
    class_paths: Sequence[Any],
    *,
    owner: str,
) -> list[str]:
    """Load and register each declared guard; *owner* names the domain in logs.

    Entries are dotted class paths (session-scoped, no-arg — the historical
    form) or declaration objects (``descriptor_from_raw`` form) whose
    ``scope: run`` entries are appended to ``system.run_descriptors`` for
    ``agent_loop`` to instantiate fresh per run. The domain channel keeps
    the no-arg construction contract — a session context is not threaded
    through activation.

    A failing declaration is logged and skipped — a broken domain guard
    must not fail the domain's activation. Returns all guard names
    (session-registered and run-scoped) for activation logging.
    """
    registered: list[str] = []
    for class_path in class_paths:
        try:
            if isinstance(class_path, str):
                guard = load_domain_guard(class_path)
            else:
                from .registry import descriptor_from_raw, load_guard_descriptor

                descriptor = descriptor_from_raw(class_path)
                if descriptor.scope == "run":
                    system.run_descriptors.append(descriptor)
                    registered.append(descriptor.name)
                    continue
                guard = load_guard_descriptor(descriptor)
        except GuardLoadError:
            logger.warning("Skipping domain guard: %s", class_path, exc_info=True)
            continue
        system.register(guard)
        registered.append(getattr(guard, "name"))
    if registered:
        logger.info("Registered domain guards %s (owner=%s)", registered, owner)
    return registered
