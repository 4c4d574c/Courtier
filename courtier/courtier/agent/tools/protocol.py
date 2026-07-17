"""Tool protocol — composable mixin protocols for tool capabilities."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Protocol, TypedDict, runtime_checkable

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from courtier.agent.tools.summary import ToolSummary

from courtier.agent.core.execution_result import ExecutionResult


class ToolResult(BaseModel):
    """Standardized tool execution result."""

    success: bool
    data: Any = None
    error: str | None = None
    metadata: dict = Field(default_factory=dict)


class ToolProgress(TypedDict):
    """Progress update emitted during tool execution."""

    status: str  # "running" | "done" | "error"
    message: str
    detail: dict | None


OnToolProgress = Callable[[ToolProgress], None]


@dataclass(frozen=True)
class ToolInfo:
    """Declarative metadata for a registered tool.

    ``ToolInfo`` is separate from ``ToolProtocol`` so versioning and
    deprecation information can be attached without changing the execution
    interface.
    """

    name: str
    version: str = "1.0.0"
    api_version: str = "1.0"
    description: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)
    deprecated: bool = False
    replaced_by: str | None = None


@runtime_checkable
class ToolProtocol(Protocol):
    """Minimal tool interface — name, description, parameters, execute."""

    name: str
    description: str
    parameters: dict  # JSON Schema

    async def execute(
        self,
        *,
        on_progress: OnToolProgress,
        **kwargs: Any,
    ) -> ToolResult | ExecutionResult:
        """Execute the tool with validated parameters."""
        ...

    def summarize(self, result: ToolResult) -> ToolSummary:
        """Optional: produce a structured display summary for TUI rendering.

        Tools override this to provide custom semantic chips and issue counts.
        Default summarizer handles dict/list/string generically.
        """
        ...


@runtime_checkable
class ToolWithSkill(Protocol):
    """Optional: tools associated with a skill workflow."""

    skill: str


@runtime_checkable
class ToolWithDisplay(Protocol):
    """Optional: tools with display metadata."""

    display_name: str | None
    output_content_type: str | None


@runtime_checkable
class ToolWithContracts(Protocol):
    """Optional: tools with input/output contracts for artifact binding."""

    input_contract: Any | None
    output_contract: Any | None


@runtime_checkable
class ToolWithRuntimePolicy(Protocol):
    """Optional: tools with runtime execution policies."""

    skip_persist: bool
    runtime_policy: Any | None
    output_schema: dict | None


@runtime_checkable
class ToolVersioned(Protocol):
    """Optional: tools that declare a version and API version."""

    version: str
    api_version: str
    deprecated: bool
    replaced_by: str | None
