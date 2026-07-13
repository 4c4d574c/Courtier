"""PromptPipeline — six-section system prompt assembly (s10 pattern).

Sections are assembled in stability order:
1. Core identity + behavioral rules  (most stable)
2. Tool list
3. Context instructions
=== DYNAMIC_BOUNDARY ===
4. Memory content
5. DRUDGE.md instruction chain
6. Environmental context            (least stable)

Learning from: https://learn.shareai.run/zh/s10/
"""

from __future__ import annotations

import logging
import platform
import re
from datetime import datetime
from typing import Any


logger = logging.getLogger(__name__)

# Maximum length for each section to prevent unbounded prompt growth
MAX_SECTION_LENGTH = 50_000

_PLACEHOLDER_RE = re.compile(r"\{\{(.+?)\}\}")
_DYNAMIC_BOUNDARY = (
    "# ====== STABLE LAYER (above) / DYNAMIC LAYER (below) ======"
)


def _resolve_placeholder(match: re.Match[str], context: dict[str, str]) -> str:
    key = match.group(1)
    value = context.get(key)
    if value is None:
        return match.group(0)
    return str(value)


class PromptPipeline:
    """六段式系统提示词组装流水线。

    核心原则：系统提示词不是一段长文本，而是把不同来源的信息按清晰边界组装起来。
    稳定块可缓存，动态块按轮次替换。
    """

    def __init__(self) -> None:
        # Structured sections (s10 six-section model)
        self._identity: str = ""
        self._behavioral_rules: str = ""
        self._tools_block: str = ""
        self._context_instructions: str = ""
        self._rules_block: str = ""
        self._memory_block: str = ""
        self._drudge_md_block: str = ""
        self._environment_block: str = ""

    def _validate_section_length(self, content: str, section_name: str) -> str:
        """Validate section content length and warn if exceeding the limit."""
        if len(content) > MAX_SECTION_LENGTH:
            logger.warning(
                "Section '%s' exceeds max length (%d > %d chars), truncating",
                section_name,
                len(content),
                MAX_SECTION_LENGTH,
            )
            return content[:MAX_SECTION_LENGTH]
        return content

    # -- Structured setters (s10 six-section model) ---------------------------

    def set_identity(
        self, name: str, role: str, thinking_directive: str = ""
    ) -> None:
        """Section 1: Core identity and behavioral instructions.

        Args:
            name: Agent display name.
            role: Agent role description / system prompt.
            thinking_directive: Thinking directive text. If empty, a
                minimal fallback is used.  Injected from PromptBundle
                rather than hardcoded.
        """
        directive = thinking_directive or (
            "# Thinking Directive\n"
            "- Be concise in your reasoning.\n"
            "- State conclusions directly.\n"
        )
        self._identity = self._validate_section_length(
            f"# 身份\n你是 {name}。\n\n# 行为规则\n{role}\n\n{directive}",
            "identity",
        )

    def set_behavioral_rules(self, rules: str) -> None:
        """Section 1.5: Behavioral rules applicable to all interactions.

        Rendered immediately after identity, before tools.  These rules
        reinforce the thinking directive with stronger, imperative language
        (e.g. "禁止...") that applies to every turn of the agent loop.
        """
        self._behavioral_rules = self._validate_section_length(
            rules, "behavioral_rules"
        )

    def set_tools(self, tools_block: str) -> None:
        """Section 2: Available tools and their descriptions."""
        self._tools_block = self._validate_section_length(
            f"# 可用工具\n{tools_block}", "tools"
        )

    def set_context_instructions(self, text: str) -> None:
        """Section 2.5: Per-session context management instructions.

        Rendered after tools.  Carries information about $ref references,
        artifact access patterns, and micro-compaction conventions that the
        LLM needs to navigate the current session.
        """
        self._context_instructions = self._validate_section_length(
            f"# 上下文规则\n{text}", "context_instructions"
        )

    def set_memory(self, memory_block: str) -> None:
        """Section 4: Cross-session retained information."""
        self._memory_block = self._validate_section_length(
            f"# 记忆\n{memory_block}", "memory"
        )

    def set_drudge_md(self, drudge_md_block: str) -> None:
        """Section 5: Long-term rule specifications (DRUDGE.md chain)."""
        self._drudge_md_block = self._validate_section_length(
            f"# 项目规则\n{drudge_md_block}", "drudge_md"
        )

    def set_environment(self, env: dict[str, str] | None = None) -> None:
        """Section 6: Dynamic environmental context (date, cwd, model, mode)."""
        env = env or {}
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        lines = [
            "# 环境上下文",
            f"- 日期: {now}",
            f"- 平台: {env.get('platform', platform.system().lower())}",
        ]
        if env.get("cwd"):
            lines.append(f"- 工作目录: {env['cwd']}")
        if env.get("model"):
            lines.append(f"- 模型: {env['model']}")
        self._environment_block = "\n".join(lines)

    def set_rules(self, rules: str) -> None:
        """Section 3: Additional rules / strategy appended after context instructions."""
        self._rules_block = self._validate_section_length(
            f"# 规则与策略\n{rules}", "rules"
        )

    # -- Build ----------------------------------------------------------------

    def build(self, context: dict[str, str] | None = None) -> str:
        """Assemble the final system prompt from all sections.

        Sections are rendered in stability order: identity → behavioral_rules
        → tools → context_instructions → rules → [dynamic boundary]
        → memory → drudge_md → environment.

        Context keys that are not consumed by {{placeholder}} substitution
        are surfaced in a final "Task Context" section so the model can
        see them (e.g. file_path).
        """
        context = context or {}
        blocks: list[str] = []
        consumed_keys: set[str] = set()

        def _resolve_and_track(m: re.Match[str]) -> str:
            consumed_keys.add(m.group(1))
            return _resolve_placeholder(m, context)

        # Stable layer
        if self._identity:
            blocks.append(self._identity)

        if self._behavioral_rules:
            blocks.append(self._behavioral_rules)

        if self._tools_block:
            blocks.append(self._tools_block)

        if self._context_instructions:
            blocks.append(self._context_instructions)

        if self._rules_block:
            resolved = _PLACEHOLDER_RE.sub(_resolve_and_track, self._rules_block)
            blocks.append(resolved)

        # Dynamic boundary
        dynamic_blocks: list[str] = []
        if self._memory_block:
            dynamic_blocks.append(self._memory_block)
        if self._drudge_md_block:
            dynamic_blocks.append(self._drudge_md_block)
        if self._environment_block:
            dynamic_blocks.append(self._environment_block)

        if dynamic_blocks:
            blocks.append(_DYNAMIC_BOUNDARY)
            blocks.extend(dynamic_blocks)

        # Surface unconsumed context values so the model sees them
        remaining = {
            k: v for k, v in context.items() if k not in consumed_keys and v
        }
        if remaining:
            lines = ["# 任务上下文"]
            for k, v in remaining.items():
                display = v if len(v) <= 500 else v[:497] + "..."
                lines.append(f"- {k}: {display}")
            blocks.append("\n".join(lines))

        return "\n\n".join(blocks)

    def build_reminder(self, turn_context: dict[str, Any] | None = None) -> str:
        """Build a per-turn system reminder — temporary context for current turn only.

        This is separate from the main system prompt. It carries transient state
        that should NOT pollute the stable prompt.
        """
        turn_context = turn_context or {}
        parts: list[str] = ["# 本轮提醒"]

        if turn_context.get("current_step"):
            parts.append(f"- 当前步骤: {turn_context['current_step']}")
        if turn_context.get("remaining_steps"):
            parts.append(f"- 剩余步骤: {turn_context['remaining_steps']}")
        if turn_context.get("note"):
            parts.append(f"- {turn_context['note']}")

        return "\n".join(parts) if len(parts) > 1 else ""
