"""Shared ToolCall dataclass used across agent core modules."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolCall:
    """A tool call the model wants to execute."""

    id: str
    name: str
    arguments: dict[str, Any]
