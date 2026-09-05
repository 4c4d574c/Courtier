"""Artifact schema registry — the typed vocabulary of artifact payloads.

Schemas describe the data shape of an artifact type.  The core package
registers only ``core.*`` types; domain packages register their own
schemas through their ``domain_artifacts`` profile module (wired by
``DomainActivator``), so the core stays domain-agnostic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import ArtifactSchema


class ArtifactSchemaRegistry:
    """Encapsulated registry of artifact type schemas.

    Deliberately not a bare module-level dict: mutation goes through
    :meth:`register`, so consumers holding a reference cannot rewrite the
    vocabulary as a side effect.
    """

    def __init__(self) -> None:
        self._schemas: dict[str, ArtifactSchema] = {}

    def register(self, type_name: str, schema: ArtifactSchema) -> None:
        """Register (or replace) the schema for *type_name* (idempotent)."""
        self._schemas[type_name] = schema

    def register_many(self, schemas: dict[str, ArtifactSchema]) -> None:
        """Register (or replace) a batch of schemas (idempotent)."""
        self._schemas.update(schemas)

    def get(self, type_name: str) -> ArtifactSchema | None:
        return self._schemas.get(type_name)

    def has(self, type_name: str) -> bool:
        return type_name in self._schemas

    def types(self) -> frozenset[str]:
        return frozenset(self._schemas)

    def __len__(self) -> int:
        return len(self._schemas)


def build_core_registry() -> "ArtifactSchemaRegistry":
    """Build a registry pre-loaded with the core ``core.*`` types."""
    from .core_schemas import register_core_schemas

    registry = ArtifactSchemaRegistry()
    register_core_schemas(registry)
    return registry
