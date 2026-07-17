"""CapabilityRegistry — unified registration for tools, skills, agents, and resources.

Centralizes extension discovery so the system no longer scatters registration
logic across ToolRegistry, SkillRegistry, ExtensionRegistry, and AgentRuntime.
A capability is a typed, named extension point with metadata and a provider.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from ..telemetry.metrics import set_capability_registry_size

logger = logging.getLogger(__name__)

CapabilityType = Literal[
    "tool",
    "skill",
    "agent",
    "resource",
    "route",
    "checker",
]


@dataclass(frozen=True)
class Capability:
    """A single registered capability.

    Attributes:
        type: The extension point this capability satisfies.
        name: Unique identifier within the capability type.
        provider: Plugin/skill/agent that owns this capability.
        title: Human-readable label.
        description: Short explanation for LLM/catalog use.
        meta: Additional type-specific metadata (schemas, policies, routes, ...).
        instance: The live object implementing the capability (tool, skill config, etc.).
    """

    type: CapabilityType
    name: str
    provider: str = "builtin"
    title: str = ""
    description: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    instance: Any | None = None

    def __post_init__(self) -> None:
        if not self.title:
            object.__setattr__(self, "title", self.name)


class CapabilityRegistry:
    """Unified registry for all agent capabilities.

    Backwards compatibility:
      - Existing ``ToolRegistry``/``SkillRegistry`` instances can be attached so
        legacy lookup paths keep working.
      - New capabilities registered directly here can be queried by type or
        resolved into the legacy registries when needed.
    """

    def __init__(
        self,
        *,
        tool_registry: Any | None = None,
        skill_registry: Any | None = None,
        agent_registry: Any | None = None,
    ) -> None:
        self._capabilities: dict[CapabilityType, dict[str, Capability]] = {
            "tool": {},
            "skill": {},
            "agent": {},
            "resource": {},
            "route": {},
            "checker": {},
        }
        self._fallback: dict[str, Any] = {
            "tool": tool_registry,
            "skill": skill_registry,
            "agent": agent_registry,
        }
        self._listeners: list[Callable[[Capability, str], None]] = []

    def add_listener(self, callback: Callable[[Capability, str], None]) -> None:
        """Register a callback invoked on register/unregister.

        The second argument is ``"register"`` or ``"unregister"``.
        """
        self._listeners.append(callback)

    def _update_metrics(self) -> None:
        """Sync per-type capability counts to the Prometheus gauge."""
        for type_ in self._capabilities:
            set_capability_registry_size(type_, len(self._capabilities[type_]))

    def register(self, capability: Capability) -> None:
        """Register a capability, replacing any existing entry with the same name."""
        by_type = self._capabilities[capability.type]
        by_type[capability.name] = capability
        self._update_metrics()
        logger.debug(
            "Registered capability %s/%s from %s",
            capability.type,
            capability.name,
            capability.provider,
        )
        for cb in self._listeners:
            try:
                cb(capability, "register")
            except Exception:
                logger.exception("Capability listener failed")

    def unregister(self, type_: CapabilityType, name: str) -> Capability | None:
        """Remove a capability and return it if it existed."""
        by_type = self._capabilities[type_]
        cap = by_type.pop(name, None)
        if cap is not None:
            self._update_metrics()
            logger.debug("Unregistered capability %s/%s", type_, name)
            for cb in self._listeners:
                try:
                    cb(cap, "unregister")
                except Exception:
                    logger.exception("Capability listener failed")
        return cap

    def unregister_by_provider(self, provider: str) -> list[Capability]:
        """Remove all capabilities owned by *provider* (e.g. plugin shutdown)."""
        removed: list[Capability] = []
        for type_ in list(self._capabilities.keys()):
            by_type = self._capabilities[type_]
            for name in list(by_type.keys()):
                cap = by_type[name]
                if cap.provider == provider:
                    by_type.pop(name, None)
                    removed.append(cap)
                    for cb in self._listeners:
                        try:
                            cb(cap, "unregister")
                        except Exception:
                            logger.exception("Capability listener failed")
        if removed:
            self._update_metrics()
        return removed

    def get(self, type_: CapabilityType, name: str) -> Capability | None:
        """Look up a capability by type and name.

        Falls back to attached legacy registries when the capability is not
        registered directly.
        """
        cap = self._capabilities[type_].get(name)
        if cap is not None:
            return cap
        fallback = self._fallback.get(type_)
        if fallback is None:
            return None
        return self._capability_from_fallback(type_, name, fallback)

    def list_capabilities(
        self,
        type_: CapabilityType | None = None,
        provider: str | None = None,
    ) -> list[Capability]:
        """Return capabilities, optionally filtered by type and/or provider."""
        types = [type_] if type_ is not None else list(self._capabilities.keys())
        result: list[Capability] = []
        for t in types:
            for cap in self._capabilities[t].values():
                if provider is None or cap.provider == provider:
                    result.append(cap)
        return result

    def list_names(self, type_: CapabilityType) -> list[str]:
        """Return all capability names of a given type."""
        return sorted(self._capabilities[type_].keys())

    def has(self, type_: CapabilityType, name: str) -> bool:
        """Return True if the capability exists (including fallback registries)."""
        return self.get(type_, name) is not None

    def build_catalog(self, type_: CapabilityType) -> str:
        """Return a markdown catalog for a capability type."""
        lines: list[str] = []
        for cap in sorted(self.list_capabilities(type_), key=lambda c: c.name):
            title = cap.title or cap.name
            lines.append(f"- **{title}**: {cap.description or 'No description'}")
        return "\n".join(lines)

    def _capability_from_fallback(
        self, type_: CapabilityType, name: str, fallback: Any
    ) -> Capability | None:
        """Convert a legacy registry entry into a Capability wrapper."""
        if type_ == "tool":
            try:
                tool = fallback.get(name)
            except Exception:
                return None
            return Capability(
                type="tool",
                name=name,
                provider="tool_registry",
                title=getattr(tool, "name", name),
                description=getattr(tool, "description", ""),
                instance=tool,
            )
        if type_ == "skill":
            skill = fallback.get(name)
            if skill is None:
                return None
            return Capability(
                type="skill",
                name=name,
                provider="skill_registry",
                title=skill.name,
                description=skill.description,
                instance=skill,
            )
        return None
