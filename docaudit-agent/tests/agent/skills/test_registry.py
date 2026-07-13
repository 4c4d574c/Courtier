from pathlib import Path

from courtier.agent.skills.registry import SkillRegistry


FIXTURES = Path(__file__).parent / "fixtures"


def test_registry_scans_valid_skill():
    registry = SkillRegistry(FIXTURES)
    registry.scan()

    config = registry.get("valid_skill")
    assert config is not None
    assert config.name == "valid_skill"
    assert config.description == "有效 Skill"
    assert config.tools == ("parse_document", "audit_format")
    assert config.skills == ()
    assert "你是格式审核专家" in config.system_prompt


def test_registry_scans_skill_with_skills_field():
    """nested_skill declares both tools and skills in frontmatter."""
    registry = SkillRegistry(FIXTURES)
    registry.scan()

    config = registry.get("nested_skill")
    assert config is not None
    assert config.name == "nested_skill"
    assert config.tools == ("echo",)
    assert config.skills == ("valid_skill",)


def test_registry_skills_without_frontmatter():
    registry = SkillRegistry(FIXTURES)
    registry.scan()

    assert registry.get("missing_frontmatter") is None
    assert any("frontmatter" in e.lower() for e in registry.errors)


def test_registry_skips_bad_yaml():
    registry = SkillRegistry(FIXTURES)
    registry.scan()

    assert registry.get("bad_yaml") is None
    assert any("yaml" in e.lower() for e in registry.errors)


def test_registry_builds_catalog():
    registry = SkillRegistry(FIXTURES)
    registry.scan()

    catalog = registry.build_catalog()
    assert "valid_skill" in catalog
    assert "有效 Skill" in catalog


def test_real_skills_include_output_sanitization():
    """Deployed skill prompts must instruct sub-agents not to leak internal names."""
    skills_dir = Path(__file__).parents[3] / "packages" / "domains" / "docaudit" / "skills"
    registry = SkillRegistry(skills_dir)
    registry.scan()

    enabled = registry.list_enabled()
    enabled_names = {s.name for s in enabled}
    expected = {"format_audit", "content_audit", "plagiarism", "full_government_audit"}
    assert expected.issubset(enabled_names), (
        f"expected skills {expected} to be enabled, got {enabled_names}; "
        f"errors: {registry.errors}"
    )
    assert not registry.errors, f"skill registry has errors: {registry.errors}"
    for skill in enabled:
        assert "最终报告中禁止出现" in skill.system_prompt, (
            f"Skill {skill.name} missing output sanitization rule"
        )
