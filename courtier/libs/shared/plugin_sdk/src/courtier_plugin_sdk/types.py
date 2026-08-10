"""Generic plugin type definitions — shared between core and domain packages.

These types live in core so that domain code can implement them and
core code can consume them without importing from any domain package.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Violation:
    """A single compliance violation record (domain-agnostic)."""

    rule_id: str
    message: str
    severity: str = "error"
    position: str | None = None


@dataclass(frozen=True)
class ComplianceResult:
    """Result of a compliance check (domain-agnostic)."""

    is_valid: bool
    violations: tuple[Violation, ...] = ()


@runtime_checkable
class ContentChecker(Protocol):
    """Content checker protocol — one implementation per document type.

    Domain packages implement this protocol.  Core consumes it through
    the plugin system without importing domain code.
    """

    @property
    def doc_type(self) -> str:
        """Supported document type identifier."""
        ...

    async def check(self, text: str, subtype: str | None = None) -> ComplianceResult:
        """Run a compliance check against the given text."""
        ...


@runtime_checkable
class CheckerRegistryLike(Protocol):
    """Structural interface for a checker registry.

    Domain packages provide concrete implementations.  Core consumes
    this through the plugin system without importing domain code.
    """

    def register(self, checker: ContentChecker) -> None: ...
    def unregister(self, doc_type: str) -> None: ...
