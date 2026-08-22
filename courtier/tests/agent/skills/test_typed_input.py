"""SkillTool typed-input wiring: schema merge, conflict detection, error template."""

from __future__ import annotations

import pytest
from pydantic import BaseModel, Field

from courtier.agent.agents.subagent.base import (
    RESERVED_INPUT_FIELDS,
    SubAgentInput,
    data_field_names,
)
from courtier.agent.skills.config import SkillConfig
from courtier.agent.skills.registry import SkillRegistry
from courtier.agent.tools.builtin.skill import SkillTool
from courtier.prompts.engine import PromptBundle, PromptEngine

from .fixtures.schemas_for_tests import ConflictingInput, DocAuditInput


def _make_config(input_model: type[BaseModel] | None) -> SkillConfig:
    return SkillConfig(
        name="doc_audit",
        description="审核文档",
        source_path=None,
        system_prompt="审核。",
        tools=(),
        skills=(),
        input_model=input_model,
        output_artifact_type=None,
        tags=(),
        enabled=True,
    )


def _tool_with_input(input_model: type[BaseModel] | None) -> SkillTool:
    # runtime is unused at construction time — schema merge happens in __init__.
    return SkillTool(skill=_make_config(input_model), runtime=None)


class TestDataFieldNames:
    def test_excludes_reserved_base_fields(self):
        class Sample(SubAgentInput):
            document: str = Field(description="doc")
            top_k: int = Field(default=5)

        assert data_field_names(Sample) == {"document", "top_k"}

    def test_reserved_set_covers_framework_plumbing(self):
        assert {"task", "ref_ids", "output_for", "explicit_inputs"} <= RESERVED_INPUT_FIELDS

    def test_real_docaudit_schema_data_fields(self):
        from skills.schemas.plagiarism import PlagiarismAuditorInput

        assert data_field_names(PlagiarismAuditorInput) == {
            "document",
            "library_docs",
            "top_k",
        }


class TestSkillToolSchemaMerge:
    def test_typed_fields_join_tool_parameters(self):
        tool = _tool_with_input(DocAuditInput)
        props = tool.parameters["properties"]
        assert "document" in props
        assert "$ref" in props["document"]["description"]
        # built-in parameters untouched
        assert tool.parameters["required"] == ["task", "mode"]
        assert "mode" in props

    def test_plain_skill_keeps_generic_parameters(self):
        tool = _tool_with_input(None)
        assert set(tool.parameters["properties"]) == {"task", "file_path", "ref_ids", "mode"}

    def test_conflicting_field_rejected_at_construction(self):
        with pytest.raises(ValueError, match="conflicts"):
            _tool_with_input(ConflictingInput)


class TestRegistryConflictDetection:
    def test_conflicting_input_model_fails_scan(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        (skills_dir / "broken.md").write_text(
            "---\n"
            "name: broken\n"
            "description: bad schema\n"
            "version: '1.0'\n"
            "input_model: tests.agent.skills.fixtures.schemas_for_tests.ConflictingInput\n"
            "---\n\nbody\n",
            encoding="utf-8",
        )
        registry = SkillRegistry(skills_dir)
        registry.scan()
        assert registry.has_errors
        assert all(c.name != "broken" for c in registry.list_enabled())
        assert any("conflict" in e for e in registry.errors)

    def test_valid_input_model_passes_scan(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        (skills_dir / "good.md").write_text(
            "---\n"
            "name: good\n"
            "description: good schema\n"
            "version: '1.0'\n"
            "input_model: tests.agent.skills.fixtures.schemas_for_tests.DocAuditInput\n"
            "---\n\nbody\n",
            encoding="utf-8",
        )
        registry = SkillRegistry(skills_dir)
        registry.scan()
        assert not registry.has_errors
        assert [c.name for c in registry.list_enabled()] == ["good"]
        # The same dotted path can yield a distinct module object under pytest's
        # import mode — compare by qualified name, not class identity.
        assert (
            registry.get("good").input_model.__qualname__ == DocAuditInput.__qualname__
        )


class TestValidationErrorMessageTemplate:
    def test_key_is_renderable_from_core_defaults(self):
        engine = PromptEngine.from_domain_directories([], locale="zh-CN")
        out = engine.render(
            "errors.skill_input_validation",
            skill_name="content_audit",
            error_details="- document：期望 string，收到 object",
        )
        assert "content_audit" in out
        assert "期望 string" in out
        assert "重新调用" in out

    def test_fallback_when_no_bundle(self):
        engine = PromptEngine(PromptBundle(locale="en-US", templates={}))
        out = engine.render(
            "errors.skill_input_validation",
            skill_name="x",
            error_details="bad",
        )
        assert "invalid input" in out
