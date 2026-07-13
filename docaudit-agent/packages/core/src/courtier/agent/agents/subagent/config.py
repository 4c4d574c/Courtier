"""Subagent configuration types and failure strategy."""

from __future__ import annotations

import types
import typing
from dataclasses import dataclass
from enum import Enum
from typing import Any

from pydantic import BaseModel

from ..base import Agent


def _annotation_expects_type(annotation: Any, expected_types: tuple[type, ...]) -> bool:
    """Check whether a type annotation expects one of the given types.

    Handles simple types, Optional[T], Union[T, None], and generic aliases.
    """
    if annotation is None:
        return False

    origin = typing.get_origin(annotation)
    # Python 3.10+ pipe syntax (str | dict) yields types.UnionType,
    # while typing.Union is used for typing.Optional / typing.Union.
    if origin is typing.Union or origin is types.UnionType:
        args = typing.get_args(annotation)
        return any(
            _annotation_expects_type(arg, expected_types)
            for arg in args
            if arg is not type(None)
        )

    if origin is not None:
        return any(
            origin is t or (isinstance(t, type) and issubclass(origin, t))
            for t in expected_types
        )

    if isinstance(annotation, type):
        return any(issubclass(annotation, t) for t in expected_types)

    return False


class FailureStrategy(str, Enum):
    STRICT = "strict"
    TOLERANT = "tolerant"
    RETRY = "retry"


@dataclass(frozen=True)
class SubAgentConfig:
    agent: Agent
    failure_strategy: FailureStrategy
    max_retries: int = 0
    timeout_seconds: float = 0  # 0 = no timeout; positive = max wall-clock seconds
    description: str = ""
    input_model: type[BaseModel] | None = (
        None  # Pydantic BaseModel subclass for structured input
    )
    output_model: type[BaseModel] | None = (
        None  # Pydantic BaseModel subclass for structured output
    )
    display_name: str = ""  # Human-readable skill name (e.g. "查重检测")
    output_artifact_type: str | None = (
        None  # Type string for output artifact registration
    )
