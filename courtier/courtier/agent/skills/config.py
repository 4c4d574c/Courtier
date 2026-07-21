from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel


class SkillMode(str, Enum):
    SEQUENTIAL = "sequential"
    PARALLEL = "parallel"
    AUTO = "auto"


class RetryPolicy(str, Enum):
    NONE = "none"
    ON_ERROR = "on_error"
    ON_TIMEOUT = "on_timeout"


@dataclass(frozen=True)
class SkillConfig:
    """预编译后的 Skill 配置。"""

    name: str
    description: str
    source_path: Path
    system_prompt: str
    tools: tuple[str, ...]  # 插件/内置工具（原子操作）
    skills: tuple[str, ...]  # 子技能（会启动子代理）
    input_model: type[BaseModel] | None
    output_artifact_type: str | None
    tags: tuple[str, ...]
    enabled: bool
    default_mode: str = ""  # "subagent" | "inline" — empty means no default; LLM must choose
    display_name: str = ""  # 中文展示名，如"格式审核"
    raw_frontmatter: dict[str, Any] = field(default_factory=dict)

    # 协议字段
    type: str = "skill"
    version: str = "1.0"
    mode: SkillMode = SkillMode.AUTO
    output_schema: str | None = None
    timeout_seconds: int = 600
    retry_policy: RetryPolicy = RetryPolicy.NONE
