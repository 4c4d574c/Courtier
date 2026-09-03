"""Input models for visual_inspection skill."""

from __future__ import annotations

from pydantic import Field, model_validator

from courtier.agent.agents.subagent.base import SubAgentInput
from courtier.agent.core.content_parts import ImageRef, VideoRef


class VisualInspectionInput(SubAgentInput):
    """视觉检查技能的结构化输入（图片/视频双模态，二选一）。"""

    image: ImageRef | None = Field(
        default=None,
        description=(
            "待检查的图片附件。填当前会话用户消息中附件（kind=image）的"
            " file_id，图片会随任务消息内联，可直接观察，无需调用工具读取。"
        ),
    )
    video: VideoRef | None = Field(
        default=None,
        description=(
            "待检查的视频附件。填当前会话用户消息中附件（kind=video）的"
            " file_id，视频已转码为 mp4 并按低帧率随任务消息内联关键帧，"
            "直接观察即可，无需调用工具读取。"
        ),
    )

    @model_validator(mode="after")
    def _require_one_medium(self) -> "VisualInspectionInput":
        if self.image is None and self.video is None:
            raise ValueError("image 与 video 至少提供一个（根据附件类型填写其一）")
        return self
