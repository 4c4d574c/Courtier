"""Input models for full_government_audit skill."""

from __future__ import annotations

from pydantic import Field

from courtier.agent.agents.subagent.base import SubAgentInput


class FullGovernmentAuditInput(SubAgentInput):
    """完整审核（复合技能）的结构化输入。

    文档本体经 file_path 透传给各子技能（format_audit / content_audit /
    plagiarism），此处只携带编排参数。
    """

    doc_type: str = Field(
        default="通知",
        description="要审核的公文文种（如：通知、函、请示、报告）",
    )
