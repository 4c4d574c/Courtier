"""Tests for DomainActivator + ActivateDomainTool (domain self-activation)."""

from __future__ import annotations

from pathlib import Path

import pytest

from courtier.agent.agents.orch import OrchestratorAgent
from courtier.agent.runtime.activation import DomainActivator
from courtier.agent.runtime.runtime import AgentRuntime
from courtier.agent.testing import MockModelClient
from courtier.agent.tools.builtin.activate_domain import ActivateDomainTool
from courtier.agent.tools.builtin.skill import SkillTool
from courtier.agent.tools.protocol import ToolResult
from courtier.agent.tools.registry import ToolRegistry
from courtier.config import CourtierConfig
from courtier.prompts.engine import PromptBundle, PromptEngine

SKILL_MD = """---
name: format_audit
type: skill
version: '1.0'
enabled: true
display_name: 格式审核
description: 按 GB/T 9704 审核公文格式
tools: [parse_document]
---

对公文进行格式审核，输出 JSON。
"""


class _PluginClient:
    def __init__(self, plugin_name: str) -> None:
        self.plugin_name = plugin_name


class _PluginTool:
    name: str = "plugin_tool"
    description: str = "fake plugin tool"
    parameters: dict = {"type": "object", "properties": {}}

    def __init__(self, name: str, plugin_name: str) -> None:
        self.name = name
        self._client = _PluginClient(plugin_name)

    async def execute(self, **kwargs):
        return ToolResult(success=True, data={})


class _BuiltinTool:
    name: str = "builtin_tool"
    description: str = "fake builtin tool"
    parameters: dict = {"type": "object", "properties": {}}

    async def execute(self, **kwargs):
        return ToolResult(success=True, data={})


class _FakePluginSystem:
    """plugin_domain mapping only — no real processes."""

    def __init__(self) -> None:
        self._domain_by_plugin = {"parse": "docaudit", "anydoc": None}
        self._scan_results = {
            "parse": _ScanResult("parse"),
            "anydoc": _ScanResult("anydoc"),
        }

    def get_scan_results(self):
        return self._scan_results

    def plugin_domain(self, name: str):
        return self._domain_by_plugin.get(name)


class _ScanResult:
    """Minimal scan-result stand-in (manifest non-None = valid plugin)."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.manifest = object()


def _build_harness(tmp_path: Path) -> tuple[OrchestratorAgent, DomainActivator, ToolRegistry]:
    """Build a gated orchestrator + activator wired to a fake domain package."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "format_audit.md").write_text(SKILL_MD, encoding="utf-8")

    model = MockModelClient(tool_calls=[])
    shared = ToolRegistry()
    shared.register(_PluginTool("domain_parse", "parse"))
    shared.register(_PluginTool("shared_convert", "anydoc"))
    shared.register(_BuiltinTool())

    runtime = AgentRuntime(tool_registry=shared, model=model, skill_registry=None)

    class _FakeDomainConfig:
        description = "公文智能审计（测试）"

    class _FakeDomain:
        name = "docaudit"
        config = _FakeDomainConfig()
        skills_path = skills_dir
        prompt_bundle = PromptBundle(
            locale="zh-CN",
            templates={"orchestrator.workflow_rules": "# 领域规则（docaudit）"},
        )

    cfg = CourtierConfig(repo_root=tmp_path, domain_names=["docaudit"])
    cfg._domains = [_FakeDomain()]  # type: ignore[attr-defined]

    plugin_system = _FakePluginSystem()
    engine = PromptEngine(PromptBundle(locale="zh-CN", templates={}))

    activate_tool = ActivateDomainTool([{"name": "docaudit", "description": "测试"}])
    agent = OrchestratorAgent(
        model=model,
        tool_registry=shared,
        skill_registry=None,
        agent_runtime=runtime,
        prompt_engine=None,
        extra_tools=[activate_tool],
        tool_filter=None,  # replaced below via activator
    )
    activator = DomainActivator(
        tool_registry=shared,
        courtier_config=cfg,
        agent_runtime=runtime,
        plugin_system=plugin_system,
        prompt_engine=engine,
        shared_plugin_names={"anydoc"},
        agent=agent,
    )
    agent._tool_filter = activator.visible
    activate_tool.set_activator(activator)
    return agent, activator, shared


class TestDomainActivator:
    @pytest.mark.asyncio
    async def test_activate_registers_skills_tools_and_rules(self, tmp_path):
        agent, activator, _shared = _build_harness(tmp_path)
        assert "format_audit" not in {t.name for t in agent.tool_registry.list_tools()}

        result = await activator.activate("docaudit")
        assert result.success is True
        assert result.new_skills == ["format_audit"]
        assert "parse" in result.new_tools
        assert activator.active_domains == {"docaudit"}

        # SkillTool registered on the agent's private registry.
        names = {t.name for t in agent.tool_registry.list_tools()}
        assert "format_audit" in names
        tool = agent.tool_registry.get("format_audit")
        assert isinstance(tool, SkillTool)
        assert tool.skill == "format_audit"

        # Runtime config registered (delegate() can spawn it).
        assert "format_audit" in agent._agent_runtime.list_agents()

        # Workflow-rules overlay landed on the prompt pipeline.
        assert "领域规则（docaudit）" in agent._prompt_pipeline._rules_block

    @pytest.mark.asyncio
    async def test_activate_is_idempotent(self, tmp_path):
        agent, activator, _ = _build_harness(tmp_path)
        await activator.activate("docaudit")
        second = await activator.activate("docaudit")
        assert second.already_active is True
        assert second.success is True
        skill_tools = [t for t in agent.tool_registry.list_tools() if isinstance(t, SkillTool)]
        assert len(skill_tools) == 1

    @pytest.mark.asyncio
    async def test_activate_unknown_domain_fails(self, tmp_path):
        _agent, activator, _ = _build_harness(tmp_path)
        result = await activator.activate("no_such_domain")
        assert result.success is False
        assert "未知领域" in result.message
        assert activator.active_domains == set()

    def test_visible_rule_tracks_active_domains_live(self, tmp_path):
        agent, activator, _ = _build_harness(tmp_path)
        # Before activation: shared plugin + builtin visible, domain tool hidden.
        assert activator.visible(agent.tool_registry.get("shared_convert")) is True
        assert activator.visible(agent.tool_registry.get("builtin_tool")) is True
        assert activator.visible(agent.tool_registry.get("domain_parse")) is False

        activator.active_domains.add("docaudit")
        assert activator.visible(agent.tool_registry.get("domain_parse")) is True


class TestActivateDomainTool:
    @pytest.mark.asyncio
    async def test_execute_activates_and_reports(self, tmp_path):
        agent, activator, _ = _build_harness(tmp_path)
        tool = ActivateDomainTool([{"name": "docaudit", "description": "测试"}])
        tool.set_activator(activator)
        result = await tool.execute(domain="docaudit")
        assert result.success is True
        assert result.data["domain"] == "docaudit"
        assert result.data["new_skills"] == ["format_audit"]
        assert "parse" in result.data["new_tools"]

    @pytest.mark.asyncio
    async def test_execute_unknown_domain_reports_error(self, tmp_path):
        _agent, activator, _ = _build_harness(tmp_path)
        tool = ActivateDomainTool([{"name": "docaudit", "description": "测试"}])
        tool.set_activator(activator)
        result = await tool.execute(domain="missing")
        assert result.success is False
        assert "未知领域" in result.error

    @pytest.mark.asyncio
    async def test_execute_without_activator_fails_cleanly(self):
        tool = ActivateDomainTool([{"name": "docaudit", "description": "测试"}])
        result = await tool.execute(domain="docaudit")
        assert result.success is False
        assert "未接线" in result.error

    @pytest.mark.asyncio
    async def test_description_embeds_domain_catalog(self):
        tool = ActivateDomainTool(
            [
                {"name": "docaudit", "description": "公文智能审计"},
                {"name": "other", "description": "其他"},
            ]
        )
        assert "docaudit" in tool.description
        assert "公文智能审计" in tool.description
        assert "other" in tool.description
        assert "疑似即激活" in tool.description


class TestOrchestratorPromptDefaults:
    def test_core_default_orchestrator_prompt_renders(self):
        engine = PromptEngine.from_domain_directories(domain_paths=[], locale="zh-CN")
        rendered = engine.render("orchestrator.system_prompt", agent_name="Courtier")
        assert "Courtier" in rendered
        assert "convert_document" in rendered
        assert "疑似即激活" in rendered
        assert "activate_domain" in rendered

    def test_domain_system_prompt_falls_back_to_core_default(self):
        """docaudit no longer ships orchestrator.system_prompt — core default wins."""
        from courtier.config import CourtierConfig

        cfg = CourtierConfig.from_env()
        engine = cfg.build_prompt_engine()
        rendered = engine.render("orchestrator.system_prompt", agent_name="Courtier")
        # Domain's old prompt said "调用合适的 Skill 工具"; the core default
        # carries the domain-agnostic activation guidance.
        assert "疑似即激活" in rendered
        assert "convert_document" in rendered
