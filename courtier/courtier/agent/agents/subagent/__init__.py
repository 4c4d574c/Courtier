"""Subagent system — configuration, structured input/output, and stream events."""

from .config import FailureStrategy, SubAgentConfig
from .events import SubAgentStreamEvent

__all__ = [
    "FailureStrategy",
    "SubAgentConfig",
    "SubAgentStreamEvent",
]
