"""Skill administration service — list, enable/disable, and create skills.

Skills are Markdown files with YAML frontmatter under the domain's
``skills/`` directory.  Enabling/disabling rewrites the ``enabled`` key in
the frontmatter in place, preserving the rest of the file byte-for-byte.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import yaml
from fastapi import HTTPException

from ...skills import SkillRegistry

logger = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_MAX_PROMPT_CHARS = 32_000
_MODES = ("", "auto", "subagent", "inline")


def _scan(skills_dir: str) -> SkillRegistry:
    registry = SkillRegistry(skills_dir)
    registry.scan()
    return registry


def list_skills(skills_dir: str) -> dict[str, Any]:
    """Scan the skills directory and return configs plus validation errors."""
    registry = _scan(skills_dir)
    items = []
    for config in sorted(registry.list_all(), key=lambda c: c.name):
        items.append(
            {
                "name": config.name,
                "displayName": config.display_name or config.name,
                "description": config.description,
                "enabled": config.enabled,
                "mode": config.mode.value if hasattr(config.mode, "value") else config.mode,
                "defaultMode": config.default_mode,
                "tools": list(config.tools),
                "skills": list(config.skills),
                "tags": list(config.tags),
                "version": config.version,
                "timeoutSeconds": config.timeout_seconds,
                "source": Path(config.source_path).name,
            }
        )
    return {
        "items": items,
        "errors": list(registry.errors) if registry.has_errors else [],
    }


def _rewrite_enabled(frontmatter: str, enabled: bool) -> str:
    value = "true" if enabled else "false"
    if re.search(r"^enabled\s*:", frontmatter, flags=re.MULTILINE):
        return re.sub(
            r"^enabled\s*:.*$",
            f"enabled: {value}",
            frontmatter,
            flags=re.MULTILINE,
        )
    return frontmatter.rstrip("\n") + f"\nenabled: {value}\n"


def set_skill_enabled(skills_dir: str, name: str, enabled: bool) -> None:
    """Toggle a skill's ``enabled`` frontmatter flag in place."""
    registry = _scan(skills_dir)
    config = next((c for c in registry.list_all() if c.name == name), None)
    if config is None:
        raise HTTPException(404, f"技能不存在: {name}")
    path = Path(config.source_path)
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise HTTPException(400, "技能文件缺少 frontmatter，无法修改")
    end = text.find("\n---", 3)
    if end < 0:
        raise HTTPException(400, "技能文件 frontmatter 格式异常")
    fm = text[3:end]
    new_text = text[:3] + _rewrite_enabled(fm, enabled) + text[end:]
    path.write_text(new_text, encoding="utf-8")


def create_skill(
    skills_dir: str,
    *,
    name: str,
    display_name: str = "",
    description: str = "",
    mode: str = "",
    default_mode: str = "",
    tools: list[str] | None = None,
    skills: list[str] | None = None,
    tags: list[str] | None = None,
    system_prompt: str = "",
    known_tools: set[str] | None = None,
) -> dict[str, Any]:
    """Create a new skill Markdown file with validated frontmatter."""
    if not _NAME_RE.match(name):
        raise HTTPException(400, "名称必须以小写字母开头，仅含小写字母/数字/下划线（≤64 字符）")
    if default_mode not in ("", "subagent", "inline"):
        raise HTTPException(400, "default_mode 必须是 subagent / inline / 空")
    if mode and mode not in _MODES:
        raise HTTPException(400, "mode 必须是 auto / subagent / inline / 空")
    if not system_prompt.strip():
        raise HTTPException(400, "工作流指令（system_prompt）不能为空")
    if len(system_prompt) > _MAX_PROMPT_CHARS:
        raise HTTPException(400, f"工作流指令过长（≤{_MAX_PROMPT_CHARS} 字符）")

    path = Path(skills_dir) / f"{name}.md"
    if path.exists():
        raise HTTPException(409, f"技能 {name} 已存在")

    registry = _scan(skills_dir)
    known_skills = {c.name for c in registry.list_all()}
    unknown_skills = sorted(set(skills or []) - known_skills)
    if unknown_skills:
        raise HTTPException(400, f"未知子技能: {', '.join(unknown_skills)}")
    unknown_tools = sorted(set(tools or []) - known_tools) if known_tools is not None else []

    frontmatter: dict[str, Any] = {
        "name": name,
        "type": "skill",
        "version": "1.0",
        "enabled": True,
    }
    if display_name:
        frontmatter["display_name"] = display_name
    if description:
        frontmatter["description"] = description
    if mode:
        frontmatter["mode"] = mode
    if default_mode:
        frontmatter["default_mode"] = default_mode
    if tools:
        frontmatter["tools"] = tools
    if skills:
        frontmatter["skills"] = skills
    if tags:
        frontmatter["tags"] = tags

    content = (
        "---\n"
        + yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False)
        + "---\n\n"
        + system_prompt.strip()
        + "\n"
    )
    path.write_text(content, encoding="utf-8")
    result: dict[str, Any] = {"name": name, "source": path.name, "enabled": True}
    if unknown_tools:
        result["warnings"] = [f"工具当前未注册（运行时可能不可用）: {', '.join(unknown_tools)}"]
    return result
