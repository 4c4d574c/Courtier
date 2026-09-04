"""Declarative guard registry: descriptors, session context, unified loading.

Guards are declared as data, not wired at construction sites:

- settings key ``guardrail_guards`` carries a list of :class:`GuardDescriptor`
  objects (name / class_path / scope / enabled / builtin);
- ``domain.yaml`` ``guards:`` declarations feed the same loader (strings =
  session-scoped no-arg guards; object form may declare ``scope: run``).

A descriptor is a *recipe*, never a shared instance: session-scoped guards
are instantiated once per ``build_agent``, run-scoped guards once per
``agent_loop`` so per-run state never mixes across the orchestrator and
nested sub-agents sharing the session system.

Construction contract: a guard class either takes no required constructor
arguments (read its configuration from settings at check time) or implements
``@classmethod build(cls, ctx: GuardSessionContext)`` to receive session-level
dependencies (workspace root, capability registry, approved tools, settings).
"""

from __future__ import annotations

import importlib
import inspect
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from .guardrail_system import SCOPES, GuardrailSystem

logger = logging.getLogger(__name__)

GuardScope = Literal["session", "run"]

#: Guard classes must expose these; ``check_call`` is optional (turn-level
#: guards only implement ``check``).
REQUIRED_GUARD_ATTRIBUTES = ("name", "layer")

_LEGAL_SCOPES = ("session", "run")


class GuardLoadError(Exception):
    """A declared guard could not be loaded or is not a guard."""


@dataclass(frozen=True)
class GuardDescriptor:
    """One declared guard entry (settings list item / domain.yaml object form).

    ``name`` is the administrative identifier (unique per declaration list);
    runtime attribution (events, metrics, audit) keeps using the guard
    instance's own ``name`` attribute. ``builtin`` marks the five seeded
    baseline entries — immutable class path/scope/name, ``enabled`` remains
    admin-controllable.
    """

    name: str
    class_path: str
    scope: GuardScope = "session"
    enabled: bool = True
    builtin: bool = False


#: The five seeded baseline guards, in dispatch order (list order = check
#: order per scope; deny-priority permission guards come before the
#: confirmation guard). Seeded into the ``guardrail_guards`` setting on
#: first start; admin may disable them (audited) but identity fields are
#: server-authoritative.
DEFAULT_GUARD_DECLARATIONS: tuple[GuardDescriptor, ...] = tuple(
    GuardDescriptor(name=name, class_path=class_path, scope=scope, enabled=True, builtin=True)
    for name, class_path, scope in (
        (
            "tool_disabled",
            "courtier.agent.core.guardrails.permission_guards.ToolDisabledGuard",
            "session",
        ),
        (
            "path_policy",
            "courtier.agent.core.guardrails.permission_guards.PathPolicyGuard",
            "session",
        ),
        (
            "confirmation",
            "courtier.agent.core.guardrails.confirmation.ConfirmationGuard",
            "session",
        ),
        ("explore_loop", "courtier.agent.core.guardrails.loop_guardrails.ExploreLoopGuard", "run"),
        (
            "business_artifact",
            "courtier.agent.core.guardrails.loop_guardrails.BusinessArtifactProgressGuard",
            "run",
        ),
    )
)


@dataclass(frozen=True)
class GuardSessionContext:
    """Session-level dependencies handed to guard factories (``build``).

    ``approved_tools`` must be the live per-session set — the confirmation
    approval path mutates it in place.
    """

    session_workspace: Path
    capability_registry: Any = None
    approved_tools: set[str] = field(default_factory=set)
    settings: Any = None


def load_guard_class(class_path: str):
    """Import and return a guard class by dotted path (no instantiation)."""
    module_path, _, class_name = class_path.rpartition(".")
    if not module_path or not class_name:
        raise GuardLoadError(f"Guard declaration must be '<module>.<Class>': {class_path!r}")
    try:
        module = importlib.import_module(module_path)
        return getattr(module, class_name)
    except (ImportError, AttributeError) as exc:
        raise GuardLoadError(f"Guard {class_path!r} could not be imported: {exc}") from exc


def check_guard_declaration(class_path: str, scope: GuardScope = "session") -> str:
    """Static shape check of a declared guard, without instantiation.

    Validates what is readable from the class itself: ``name``/``layer``
    attributes, legal layer, ``tool_call`` never run-scoped, and — for
    classes without a ``build`` factory — a no-required-argument
    constructor. Returns the class-level guard name. Used by the settings
    save validator and domain prevalidation.
    """
    guard_class = load_guard_class(class_path)
    name = getattr(guard_class, "name", None)
    if not isinstance(name, str) or not name:
        raise GuardLoadError(f"Guard {class_path!r} lacks a class-level string 'name'")
    layer = getattr(guard_class, "layer", None)
    if layer not in SCOPES.values():
        raise GuardLoadError(
            f"Guard {class_path!r} declares illegal layer {layer!r}; "
            f"expected one of {sorted(SCOPES.values())}"
        )
    if layer == "tool_call" and scope == "run":
        raise GuardLoadError(
            f"Guard {class_path!r}: tool_call-layer guards must be session-scoped "
            "(per-call decisions are pure in-memory and shared with sub-agents)"
        )
    build = getattr(guard_class, "build", None)
    if not callable(build):
        try:
            signature = inspect.signature(guard_class)
        except (TypeError, ValueError):  # exotic constructors: allow, runtime re-checks
            signature = None
        if signature is not None:
            for param in signature.parameters.values():
                if param.default is inspect.Parameter.empty and param.kind in (
                    inspect.Parameter.POSITIONAL_ONLY,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    inspect.Parameter.KEYWORD_ONLY,
                ):
                    raise GuardLoadError(
                        f"Guard {class_path!r} requires constructor arguments; "
                        "implement build(session_ctx) or give every argument a default"
                    )
    return name


def instantiate_guard(class_path: str, session_ctx: GuardSessionContext | None = None):
    """Import and construct a guard: ``build(ctx)`` when available, else no-arg.

    Instance-level attribute checks mirror the historical domain guard
    loader (``name``, legal ``layer``).
    """
    guard_class = load_guard_class(class_path)
    build = getattr(guard_class, "build", None)
    try:
        if callable(build) and session_ctx is not None:
            guard = build(session_ctx)
        else:
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


def load_guard_descriptor(
    descriptor: GuardDescriptor,
    session_ctx: GuardSessionContext | None = None,
):
    """Instantiate a descriptor's guard and enforce the scope/layer contract."""
    guard = instantiate_guard(descriptor.class_path, session_ctx)
    if descriptor.scope == "run" and getattr(guard, "layer") == "tool_call":
        raise GuardLoadError(
            f"Guard {descriptor.class_path!r}: tool_call-layer guards must be "
            "session-scoped (per-call decisions are pure in-memory and shared "
            "with sub-agents)"
        )
    return guard


def descriptor_from_raw(raw: Any) -> GuardDescriptor:
    """Parse and structurally validate one raw declaration (settings item)."""
    if not isinstance(raw, dict):
        raise GuardLoadError(f"Guard declaration must be an object with name/class_path: {raw!r}")
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise GuardLoadError(f"Guard declaration lacks a non-empty 'name': {raw!r}")
    class_path = raw.get("class_path")
    if not isinstance(class_path, str) or not class_path.strip():
        raise GuardLoadError(f"Guard declaration {name!r} lacks a non-empty 'class_path'")
    scope = raw.get("scope", "session")
    if scope not in _LEGAL_SCOPES:
        raise GuardLoadError(
            f"Guard declaration {name!r} has illegal scope {scope!r}; "
            f"expected one of {list(_LEGAL_SCOPES)}"
        )
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise GuardLoadError(f"Guard declaration {name!r} has non-boolean 'enabled'")
    builtin = raw.get("builtin", False)
    if not isinstance(builtin, bool):
        raise GuardLoadError(f"Guard declaration {name!r} has non-boolean 'builtin'")
    return GuardDescriptor(
        name=name.strip(),
        class_path=class_path.strip(),
        scope=scope,
        enabled=enabled,
        builtin=builtin,
    )


def wire_guard_declarations(
    system: GuardrailSystem,
    declarations: Any,
    session_ctx: GuardSessionContext | None = None,
) -> list[GuardDescriptor]:
    """Session assembly for a declaration list (settings ``guardrail_guards``).

    Enabled session-scope guards are instantiated (in list order) and
    registered into *system*; enabled run-scope descriptors are returned for
    ``agent_loop`` to instantiate fresh per run. A broken declaration is
    logged and skipped — mirroring the domain channel, a bad guard must not
    fail the session build. Entries may be ``GuardDescriptor`` instances or
    any duck-typed declaration exposing the same five attributes.
    """
    run_descriptors: list[GuardDescriptor] = []
    for declaration in declarations or []:
        descriptor = (
            declaration
            if isinstance(declaration, GuardDescriptor)
            else GuardDescriptor(
                name=getattr(declaration, "name", ""),
                class_path=getattr(declaration, "class_path", ""),
                scope=getattr(declaration, "scope", "session"),
                enabled=getattr(declaration, "enabled", True),
                builtin=getattr(declaration, "builtin", False),
            )
        )
        if not descriptor.enabled:
            continue
        if descriptor.scope == "run":
            run_descriptors.append(descriptor)
            continue
        try:
            system.register(load_guard_descriptor(descriptor, session_ctx))
        except GuardLoadError:
            logger.warning(
                "Skipping declared guard %s (%s)",
                descriptor.name,
                descriptor.class_path,
                exc_info=True,
            )
    return run_descriptors
