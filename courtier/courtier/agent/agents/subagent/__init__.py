"""Subagent system — configuration, structured input/output, and stream events."""

from .config import FailureStrategy
from .events import SubAgentStreamEvent

__all__ = [
    "FailureStrategy",
    "SubAgentStreamEvent",
]
