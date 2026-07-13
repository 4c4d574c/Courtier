from pathlib import Path

from courtier.agent.skills.config import (
    RetryPolicy,
    SkillConfig,
    SkillMode,
)


def test_skill_config_creation():
    config = SkillConfig(
        name="format_audit",
        description="格式审核",
        source_path=Path("skills/format_audit.md"),
        system_prompt="你是格式审核专家",
        tools=("parse_document", "audit_format"),
        skills=("content_audit",),
        input_model=None,
        output_artifact_type="format_audit_result",
        tags=("audit",),
        enabled=True,
        raw_frontmatter={"name": "format_audit"},
    )

    assert config.name == "format_audit"
    assert config.tools == ("parse_document", "audit_format")
    assert config.skills == ("content_audit",)
    assert config.enabled is True


def test_skill_config_default_mode():
    """default_mode should default to empty string when not explicitly set (LLM must choose)."""
    config = SkillConfig(
        name="test",
        description="test",
        source_path=Path("skills/test.md"),
        system_prompt="test",
        tools=(),
        skills=(),
        input_model=None,
        output_artifact_type=None,
        tags=(),
        enabled=True,
    )
    assert config.default_mode == ""


def test_skill_config_explicit_mode():
    """default_mode should accept "inline" when explicitly set."""
    config = SkillConfig(
        name="test",
        description="test",
        source_path=Path("skills/test.md"),
        system_prompt="test",
        tools=(),
        skills=(),
        input_model=None,
        output_artifact_type=None,
        tags=(),
        enabled=True,
        default_mode="inline",
    )
    assert config.default_mode == "inline"


def test_skill_config_new_fields_defaults():
    """New protocol fields should have sensible defaults for backward compat."""
    config = SkillConfig(
        name="test",
        description="test",
        source_path=Path("skills/test.md"),
        system_prompt="test",
        tools=(),
        skills=(),
        input_model=None,
        output_artifact_type=None,
        tags=(),
        enabled=True,
    )
    assert config.type == "skill"
    assert config.version == "1.0"
    assert config.mode == SkillMode.AUTO
    assert config.output_schema is None
    assert config.timeout_seconds == 600
    assert config.retry_policy == RetryPolicy.NONE


def test_skill_config_new_fields_explicit():
    """New protocol fields should accept explicit values."""
    config = SkillConfig(
        name="test",
        description="test",
        source_path=Path("skills/test.md"),
        system_prompt="test",
        tools=(),
        skills=(),
        input_model=None,
        output_artifact_type=None,
        tags=(),
        enabled=True,
        type="skill",
        version="2.0",
        mode=SkillMode.PARALLEL,
        output_schema="MyOutput",
        timeout_seconds=300,
        retry_policy=RetryPolicy.ON_ERROR,
    )
    assert config.type == "skill"
    assert config.version == "2.0"
    assert config.mode == SkillMode.PARALLEL
    assert config.output_schema == "MyOutput"
    assert config.timeout_seconds == 300
    assert config.retry_policy == RetryPolicy.ON_ERROR
