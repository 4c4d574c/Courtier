"""Typed input models used by SkillTool typed-input tests."""

from __future__ import annotations

from pydantic import Field

from courtier.agent.agents.subagent.base import SubAgentInput


class DocAuditInput(SubAgentInput):
    """Well-formed data payload: one string document field."""

    document: str = Field(
        description="待审文档文本。可传 $ref:convert_document:1 引用，系统自动解析为全文。"
    )


class ConflictingInput(SubAgentInput):
    """Declares a field clashing with a built-in SkillTool parameter."""

    mode: str = Field(description="conflicts with the built-in mode parameter")


class OptionalFieldsInput(SubAgentInput):
    """Data payload with an optional field that stays unset by default."""

    document: str = Field(description="doc text")
    library_docs: list[str] | None = Field(default=None)
