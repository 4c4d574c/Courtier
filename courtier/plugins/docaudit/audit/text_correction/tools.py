"""Text correction tool — wraps doccorrector.corrector for JSON-RPC access."""

from __future__ import annotations

from typing import Any

from courtier.agent.tools.protocol import ToolResult
from doccorrector.corrector import ErrorCorrect


class TextCorrectionTool:
    """Detect and correct Chinese text errors."""

    name: str = "correct_text"
    description: str = (
        "Detect and correct Chinese spelling, grammar, fullwidth/halfwidth character "
        "issues, and punctuation mixing errors. Returns a list of corrections with "
        "original text, corrected text, and per-sentence error details."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "Text to check and correct.",
            },
        },
        "required": ["text"],
    }

    def __init__(self) -> None:
        self._corrector: ErrorCorrect | None = None

    def _get_corrector(self) -> ErrorCorrect:
        if self._corrector is None:
            self._corrector = ErrorCorrect()
        return self._corrector

    async def execute(self, text: str, **kwargs: Any) -> ToolResult:
        """Execute text correction."""
        try:
            corrector = self._get_corrector()
            results = corrector.infer([text])
            return ToolResult(success=True, data={"results": results})
        except Exception as exc:
            return ToolResult(success=False, error=str(exc))
