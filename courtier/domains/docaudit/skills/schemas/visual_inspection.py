"""Input models for visual_inspection skill."""

from __future__ import annotations

from pydantic import Field

from courtier.agent.agents.subagent.base import SubAgentInput
from courtier.agent.core.content_parts import ImageRef


class VisualInspectionInput(SubAgentInput):
    """图片视觉检查技能的结构化输入。"""

    image: ImageRef = Field(
        description=(
            "待检查的图片附件。填当前会话用户消息中附件（kind=image）的"
            " file_id，图片会随任务消息内联，可直接观察，无需调用工具读取。"
        )
    )
