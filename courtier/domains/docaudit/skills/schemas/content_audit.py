"""Input models for content_audit skill."""

from __future__ import annotations

from pydantic import Field

from courtier.agent.agents.subagent.base import SubAgentInput


class ContentAuditInput(SubAgentInput):
    """内容审核与纠错技能的结构化输入。"""

    document: str = Field(
        description=(
            "待审核的文档正文。直接传入 $ref:convert_document:1 引用"
            "（Markdown 自动投影为纯文本），系统在派发前解析为完整文本；"
            "不要先 get_artifact 读回全文再粘贴。"
        )
    )
