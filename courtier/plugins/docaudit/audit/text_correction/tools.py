"""Text correction tool — wraps doccorrector.corrector for JSON-RPC access."""

from __future__ import annotations

import os
from typing import Any

from courtier_plugin_sdk import ToolResult
from doccorrector.corrector import ErrorCorrect


class TextCorrectionTool:
    """Detect and correct Chinese text errors."""

    name: str = "correct_text"
    display_name: str | None = "文本纠错"
    description: str = (
        "Detect and correct Chinese spelling, grammar, fullwidth/halfwidth character "
        "issues, and punctuation mixing errors. Each result contains source, target, "
        "and errors: target is the fully corrected text with every listed error "
        "applied (errors is the complete source→target diff, including delete and "
        "insert operations), so errors and target are always consistent."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": (
                    "Text to check and correct. Accepts a $ref:<tool>:<n> "
                    "result_id — the wrapped text is injected automatically; "
                    "do not paste the full document yourself."
                ),
            },
        },
        "required": ["text"],
    }

    def __init__(self) -> None:
        self._corrector: ErrorCorrect | None = None

    def _get_corrector(self) -> ErrorCorrect:
        if self._corrector is None:
            # CEC_* vars are injected by the host from plugin.yaml runtime.env.
            self._corrector = ErrorCorrect(
                api_base=os.environ.get("CEC_API_BASE", ""),
                api_key=os.environ.get("CEC_API_KEY", ""),
                model_name=os.environ.get("CEC_MODEL_NAME", "ChineseErrorCorrector3-4B"),
                max_length=int(os.environ.get("CEC_MAX_LENGTH") or "16383"),
                user_dict=os.environ.get("CEC_USER_DICT", ""),
            )
        return self._corrector

    async def execute(self, text: str, **kwargs: Any) -> ToolResult:
        """Execute text correction."""
        try:
            corrector = self._get_corrector()
            results = corrector.infer([text])
            return ToolResult(success=True, data={"results": results})
        except Exception as exc:
            return ToolResult(success=False, error=str(exc))
