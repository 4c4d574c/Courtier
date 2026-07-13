"""Input/Output Pydantic models for sub-agent typed task passing."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import Field

from .subagent.base import SubAgentInput, SubAgentOutput

# Skill-specific auditor inputs now live in skills/schemas. They are re-exported
# here for backward compatibility, but resolved lazily (PEP 562 __getattr__) to
# avoid an import cycle: each skills.schemas.* module imports SubAgentInput from
# this package, so an eager re-export here would re-enter a partially initialized
# schema module while the agents package is still importing.
_SKILL_INPUT_EXPORTS = {
    "FormatAuditorInput": "skills.schemas.format_audit",
    "PlagiarismAuditorInput": "skills.schemas.plagiarism",
}

if TYPE_CHECKING:
    from skills.schemas.format_audit import FormatAuditorInput
    from skills.schemas.plagiarism import PlagiarismAuditorInput

__all__ = [
    "SubAgentInput",
    "SubAgentOutput",
    "FormatAuditorInput",
    "PlagiarismAuditorInput",
    "FormatAuditOutput",
    "ContentAuditOutput",
    "CorrectionAuditOutput",
    "PlagiarismAuditOutput",
    "StyleAuditOutput",
]


# -- Output Models -------------------------------------------------------------


class FormatAuditOutput(SubAgentOutput):
    """格式审计结果。"""

    doc_type: str | None = Field(default=None, description="检测到的文档类型")
    errors: list[dict[str, Any]] = Field(
        default_factory=list, description="格式错误列表"
    )


class ContentAuditOutput(SubAgentOutput):
    """内容审计结果。"""

    domain: str = Field(default="通用", description="合规域")
    violations: list[dict[str, Any]] = Field(
        default_factory=list, description="违规项列表"
    )


class CorrectionAuditOutput(SubAgentOutput):
    """文字纠错审计结果。"""

    corrections: list[dict[str, Any]] = Field(
        default_factory=list, description="纠错项列表"
    )


class PlagiarismAuditOutput(SubAgentOutput):
    """抄袭检测审计结果。"""

    is_plagiarism: bool = Field(default=False, description="是否检测到抄袭")
    matches: list[dict[str, Any]] = Field(
        default_factory=list, description="匹配片段列表"
    )


class StyleAuditOutput(SubAgentOutput):
    """行文风格审计结果。"""

    is_valid: bool = Field(default=True, description="风格是否合规")
    violations: list[dict[str, Any]] = Field(
        default_factory=list, description="违规项列表"
    )


def __getattr__(name: str) -> Any:
    """Lazily resolve backward-compatible auditor-input re-exports (PEP 562)."""
    module_path = _SKILL_INPUT_EXPORTS.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    return getattr(import_module(module_path), name)
