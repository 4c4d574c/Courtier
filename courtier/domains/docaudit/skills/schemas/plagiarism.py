"""Input models for plagiarism skill."""

from __future__ import annotations

from pydantic import Field

from courtier.agent.agents.subagent.base import SubAgentInput


class PlagiarismAuditorInput(SubAgentInput):
    """抄袭检测审计器的结构化输入。"""

    document: str | dict = Field(
        description=(
            "待检测的文档数据。可传入 convert_document 的 $ref:convert_document:1 引用"
            "（Markdown，自动投影为纯文本），或 parse_layout 的 $ref:parse_layout:1 引用"
        )
    )
    library_docs: list[str] | None = Field(
        default=None,
        description="参考库文档（文本列表），为空时子代理自行检索索引库",
    )
    top_k: int = Field(default=5, description="检索参考文档数量")
