"""Input models for format_audit skill."""

from __future__ import annotations

from pydantic import Field

from courtier.agent.agents.subagent.base import SubAgentInput


class FormatAuditorInput(SubAgentInput):
    """格式审计器的结构化输入。"""

    document: str | dict = Field(
        description="待审计的文档数据。可传入 $ref:parse_document:1 引用"
    )
    doc_type: str = Field(
        default="通知",
        description="要验证的文档类型（如：通知、函、请示）",
    )
