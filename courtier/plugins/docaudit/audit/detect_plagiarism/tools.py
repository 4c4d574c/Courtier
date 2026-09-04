"""Plagiarism detection tools — wrap plagiarism similarity/comparison/detection functions."""

from __future__ import annotations

from typing import Any

from core import detect_plagiarism
from courtier_plugin_sdk import InputField, ToolResult


class DetectPlagiarismTool:
    """Detect plagiarism by comparing a document against a reference library."""

    name: str = "detect_plagiarism"
    display_name: str | None = "抄袭检测"
    description: str = (
        "将文档与参考库比对进行抄袭检测，采用动态 IQR 阈值。返回 "
        "{is_plagiarism, max_similarity, dynamic_threshold, matched_doc_index, "
        "matched_substring_length, matched_text, reason}。"
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "new_doc": {
                "type": "string",
                "description": "待检测文档的全文。",
            },
            "library_docs": {
                "type": "array",
                "items": {"type": "string"},
                "description": "参考库文档文本列表。",
            },
            "k": {
                "type": "number",
                "description": "动态阈值的 IQR 系数。",
                "default": 1.5,
            },
            "min_substring_length": {
                "type": "integer",
                "description": "判定命中的最小子串长度。",
                "default": 20,
            },
        },
        "required": ["new_doc", "library_docs"],
    }
    input_fields: tuple[InputField, ...] = (
        InputField(name="new_doc", artifact_type="core.plain_text", materialize_as="string"),
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
