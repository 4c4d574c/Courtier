"""Plagiarism detection tools — wrap plagiarism similarity/comparison/detection functions."""

from __future__ import annotations

from typing import Any

from courtier.agent.artifacts.models import InputField
from courtier.agent.tools.protocol import ToolResult
from core import detect_plagiarism


class DetectPlagiarismTool:
    """Detect plagiarism by comparing a document against a reference library."""

    name: str = "detect_plagiarism"
    description: str = (
        "Compare a document against a reference library to detect plagiarism. "
        "Uses dynamic IQR thresholding. Returns {is_plagiarism, max_similarity, "
        "dynamic_threshold, matched_doc_index, matched_substring_length, "
        "matched_text, reason}."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "new_doc": {
                "type": "string",
                "description": "Full text of the document to check.",
            },
            "library_docs": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of reference library document texts.",
            },
            "k": {
                "type": "number",
                "description": "IQR multiplier for dynamic threshold.",
                "default": 1.5,
            },
            "min_substring_length": {
                "type": "integer",
                "description": "Minimum substring length to consider a match.",
                "default": 20,
            },
        },
        "required": ["new_doc", "library_docs"],
    }
    input_fields: tuple[InputField, ...] = (
        InputField(
            name="new_doc", artifact_type="core.plain_text", materialize_as="string"
        ),
        InputField(
            name="library_docs",
            artifact_type="core.text_collection",
            materialize_as="list_string",
        ),
    )
    output_artifact_type: str | None = "docaudit.plagiarism_report"

    async def execute(self, **kwargs: Any) -> ToolResult:
        try:
            result = detect_plagiarism(
                new_doc=kwargs["new_doc"],
                library_docs=kwargs["library_docs"],
                k=kwargs.get("k", 1.5),
                min_substring_length=kwargs.get("min_substring_length", 20),
            )
            return ToolResult(
                success=True,
                data={
                    "is_plagiarism": result.is_plagiarism,
                    "max_similarity": result.max_similarity,
                    "dynamic_threshold": result.dynamic_threshold,
                    "matched_doc_index": result.matched_doc_index,
                    "matched_substring_length": result.matched_substring_length,
                    "matched_text": result.matched_text,
                    "reason": result.reason,
                },
            )
        except Exception as exc:
            return ToolResult(success=False, error=str(exc))


# Factory functions
def create_detect_plagiarism_tool() -> DetectPlagiarismTool:
    return DetectPlagiarismTool()
