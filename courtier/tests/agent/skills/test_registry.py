from pathlib import Path

from courtier.agent.agents.base import Agent
from courtier.agent.skills.registry import SkillRegistry
from courtier.agent.testing import MockModelClient
from courtier.prompts.engine import PromptEngine

FIXTURES = Path(__file__).parent / "fixtures"


def test_registry_scans_valid_skill():
    registry = SkillRegistry(FIXTURES)
    registry.scan()

    config = registry.get("valid_skill")
    assert config is not None
    assert config.name == "valid_skill"
    assert config.description == "有效 Skill"
    assert config.tools == ("parse_layout", "audit_format")
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


def test_real_skills_enabled_set():
    """The deployed skill set scans cleanly; disabled skills stay loadable."""
    skills_dir = Path(__file__).parents[3] / "domains" / "docaudit" / "skills"
    registry = SkillRegistry(skills_dir)
    registry.scan()

    enabled = registry.list_enabled()
    enabled_names = {s.name for s in enabled}
    # full_government_audit 被有意停用（frontmatter enabled: false），当前启用集为以下三个。
    expected = {"format_audit", "content_audit", "plagiarism"}
    assert expected.issubset(enabled_names), (
        f"expected skills {expected} to be enabled, got {enabled_names}; "
        f"errors: {registry.errors}"
    )
    # text_correction 已并入 content_audit（内容审核与纠错），不应再作为独立技能存在。
    assert "text_correction" not in enabled_names
    # 停用的技能仍应能正常扫描，只是不出现在启用集中。
    assert "full_government_audit" not in enabled_names
    assert registry.get("full_government_audit") is not None
    assert not registry.errors, f"skill registry has errors: {registry.errors}"


def test_skill_subagents_include_output_sanitization():
    """Deployed skill sub-agent prompts must forbid leaking internal names.

    The rule is injected platform-wide via ``behavioral.rules`` when the
    Agent is constructed (AgentRuntime builds skill sub-agents the same
    way), so skill files no longer carry it themselves.
    """
    skills_dir = Path(__file__).parents[3] / "domains" / "docaudit" / "skills"
    registry = SkillRegistry(skills_dir)
    registry.scan()
    assert not registry.errors, f"skill registry has errors: {registry.errors}"

    for skill in registry.list_enabled():
        agent = Agent(
            name=skill.name,
            role=skill.system_prompt,
            tools=[],
            model=MockModelClient(),
        )
        prompt = agent.build_system_prompt()
        assert (
            "禁止在最终输出中暴露任何内部实现细节" in prompt
        ), f"Skill {skill.name} sub-agent prompt missing output sanitization rule"


def test_core_default_templates_include_output_sanitization():
    """Core default ``behavioral.rules`` (both locales) is the single source
    of the output-sanitization rule injected into every agent."""
    zh = PromptEngine.from_domain_directories([], locale="zh-CN").render("behavioral.rules")
    assert "禁止在最终输出中暴露任何内部实现细节" in zh
    en = PromptEngine.from_domain_directories([], locale="en-US").render("behavioral.rules")
    assert "Never expose internal implementation details" in en
