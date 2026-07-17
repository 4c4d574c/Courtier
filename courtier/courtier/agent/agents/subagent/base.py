"""Base input/output models for sub-agents.

These live in their own module to avoid circular imports between
framework code and skill-specific schema modules.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class SubAgentInput(BaseModel):
    """所有子代理输入的基础模型。"""

    task: str = Field(description="子代理需要完成的任务描述（自然语言）")
    explicit_inputs: dict[str, str] | None = Field(
        default=None,
        description=(
            "显式的 ref_id → 参数名映射。"
            "key 为参数名，value 为 $ref:xxx 引用字符串。"
            "系统在 dispatch 前自动将 ref 解析后的数据注入到对应字段。"
        ),
    )
    output_for: str | None = Field(
        default=None,
        description=(
            "目标下游工具名称。设置后，系统自动注入该工具的输入 schema，"
            "引导 LLM 以匹配格式输出结果。"
        ),
    )
    ref_ids: list[str] | None = Field(
        default=None,
        description=(
            "可用的缓存引用 ID 列表。系统在 dispatch 前将这些引用提示"
            "注入 task 文本，告知 LLM 哪些缓存数据可作为工具参数使用。"
        ),
    )


class SubAgentOutput(BaseModel):
    """所有子代理输出的基础模型。"""

    summary: str = Field(description="执行结果摘要")
