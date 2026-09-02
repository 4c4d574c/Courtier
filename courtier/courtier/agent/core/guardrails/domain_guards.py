"""Domain-contributed guardrails: load declared guard classes by path.

A domain package may declare in-process guardrails in its domain.yaml
(``guards: ["<module>.<Class>", ...]``). Guards are loaded at activation
time, registered into the session GuardrailSystem under the owner tag
``domain:<name>``, and never touch the network or the filesystem at check
time — guards participating in the ``tool_call`` layer must be pure
in-memory decisions (see guardrails/base module docstring).
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Sequence

from .guardrail_system import SCOPES, GuardrailSystem

logger = logging.getLogger(__name__)

#: Guard classes must expose these; ``check_call`` is optional (turn-level
#: guards only implement ``check``).
REQUIRED_GUARD_ATTRIBUTES = ("name", "layer")


class GuardLoadError(Exception):
    """A declared domain guard could not be loaded or is not a guard."""


def load_domain_guard(class_path: str):
    """Import and instantiate a declared guard class (no-arg constructor)."""
    module_path, _, class_name = class_path.rpartition(".")
    if not module_path or not class_name:
        raise GuardLoadError(f"Guard declaration must be '<module>.<Class>': {class_path!r}")
    try:
        module = importlib.import_module(module_path)
        guard_class = getattr(module, class_name)
    except (ImportError, AttributeError) as exc:
        raise GuardLoadError(f"Guard {class_path!r} could not be imported: {exc}") from exc
    try:
        guard = guard_class()
    except Exception as exc:
        raise GuardLoadError(f"Guard {class_path!r} could not be instantiated: {exc}") from exc
    for attr in REQUIRED_GUARD_ATTRIBUTES:
        if not hasattr(guard, attr):
            raise GuardLoadError(f"Guard {class_path!r} lacks required attribute {attr!r}")
    if getattr(guard, "layer") not in SCOPES.values():
        raise GuardLoadError(
            f"Guard {class_path!r} declares illegal layer {getattr(guard, 'layer')!r}; "
            f"expected one of {sorted(SCOPES.values())}"
        )
    return guard


def register_domain_guards(
    system: GuardrailSystem,
    class_paths: Sequence[str],
    *,
    owner: str,
) -> list[str]:
    """Load and register each declared guard under *owner*.

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
        system.register(guard, owner=owner)
        registered.append(getattr(guard, "name"))
    if registered:
        logger.info("Registered domain guards %s (owner=%s)", registered, owner)
    return registered
