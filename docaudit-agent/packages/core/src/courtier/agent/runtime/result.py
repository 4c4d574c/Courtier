"""ExecutionResult — compatibility re-export.

The canonical implementation now lives in ``src.agent.core.execution_result``
so that the agent state module can import it without creating a cycle with the
runtime package.
"""

from __future__ import annotations

from courtier.agent.core.execution_result import ExecutionResult

__all__ = ["ExecutionResult"]
