"""EchoTool — echoes back the input text. Used for Phase 0 verification."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..protocol import OnToolProgress, ToolResult

if TYPE_CHECKING:
    from ..summary import ToolSummary


class EchoTool:
    """Echo back the input text unchanged."""

    name: str = "echo"
    display_name: str | None = "回显"
    description: str = "Echo back the provided text exactly as given."
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "The text to echo back",
            }
        },
        "required": ["text"],
    }

    async def execute(
        self, *, on_progress: OnToolProgress, **kwargs: Any
    ) -> ToolResult:
        on_progress(
            {"status": "running", "message": "开始执行 echo...", "detail": None}
        )
        text: str = kwargs.get("text", "")
        on_progress({"status": "done", "message": "执行完成", "detail": None})
        return ToolResult(success=True, data=text)

    def summarize(self, result: ToolResult) -> "ToolSummary":
        from courtier.agent.tools.summary import summarize_result

        return summarize_result(result)
