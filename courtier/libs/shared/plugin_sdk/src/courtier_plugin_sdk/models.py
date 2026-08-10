"""Shared data models for plugin tools.

These types are defined in the SDK (not in the core application) so that
plugins can depend on them without pulling in the entire host.  The core
re-imports them, keeping a single definition for both sides of the
JSON-RPC boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field


class ToolResult(BaseModel):
    """Standardized tool execution result."""

    success: bool
    data: Any = None
    error: str | None = None
    metadata: dict = Field(default_factory=dict)


@dataclass(frozen=True)
class InputField:
    """A single input field requirement for a tool.

    Declares that the tool's parameter ``name`` should be auto-bound
    from an artifact of ``artifact_type``.
    """

    name: str
    artifact_type: str
    materialize_as: str | None = None  # derived from artifact_type if None
    required: bool = True
    constraints: dict[str, Any] = field(default_factory=dict)
