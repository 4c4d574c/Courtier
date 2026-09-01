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
tools: [parse_layout]
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
    skills_dir.mkdir(exist_ok=True)
    skill_file = skills_dir / "format_audit.md"
    if not skill_file.exists():
        skill_file.write_text(SKILL_MD, encoding="utf-8")

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
        # Tool names (not plugin names) are reported.
        assert result.new_tools == ["domain_parse"]
        assert "parse" not in result.new_tools
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
    async def test_edited_skill_fields_apply_on_next_activation(self, tmp_path):
        """In-place edits to a skill (mode, default_mode, body) take effect
        without a backend restart: the next request's activator re-scans
        the skills dir and rebuilds SkillTools from the fresh config."""
        agent, activator, _ = _build_harness(tmp_path)
        await activator.activate("docaudit")
        tool = agent.tool_registry.get("format_audit")
        assert tool.default_mode == ""  # SKILL_MD declares no default_mode
        assert "输出 JSON。" in tool._skill.system_prompt

        # The admin update path: rewrite the file in place.
        skill_file = tmp_path / "skills" / "format_audit.md"
        edited = (
            SKILL_MD.replace(
                "display_name: 格式审核", "display_name: 格式审核\ndefault_mode: inline"
            ).replace("输出 JSON。", "输出 JSON v2。")
        )
        skill_file.write_text(edited, encoding="utf-8")

        # Next request: fresh harness (new activator instance) over the same
        # on-disk skill — the edit is picked up.
        agent2, activator2, _ = _build_harness(tmp_path)
        await activator2.activate("docaudit")
        tool2 = agent2.tool_registry.get("format_audit")
        assert tool2.default_mode == "inline"
        assert "输出 JSON v2。" in tool2._skill.system_prompt

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
        assert result.data["new_tools"] == ["domain_parse"]
        # Skills and tools are labelled distinctly in the message.
        assert "新增技能（任务级工作流" in result.data["message"]
        assert "新增工具（原子能力" in result.data["message"]

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


class TestBuildDomainCatalog:
    """activate_domain 目录动态化：技能清单随 skills/ 目录实时生成。"""

    def _skills_dir(self, tmp_path: Path, *skills: str) -> Path:
        d = tmp_path / "skills"
        d.mkdir(exist_ok=True)
        for i, name in enumerate(skills):
            (d / f"{name}.md").write_text(
                f"---\nname: {name}\n"
                f"description: 技能{i}描述\n"
                f"type: skill\nversion: '1.0'\nenabled: true\n---\n\n正文。",
                encoding="utf-8",
            )
        return d

    def _config_with(self, skills_path: Path) -> CourtierConfig:
        class _FakeDomainConfig:
            description = "领域描述"

        class _FakeDomain:
            name = "docaudit"
            config = _FakeDomainConfig()

        domain = _FakeDomain()
        domain.skills_path = skills_path  # type: ignore[attr-defined]
        cfg = CourtierConfig(repo_root=skills_path.parent, domain_names=["docaudit"])
        cfg._domains = [domain]  # type: ignore[attr-defined]
        return cfg

    def test_catalog_lists_skills_with_descriptions(self, tmp_path):
        from courtier.agent.runtime.activation import _CATALOG_CACHE, build_domain_catalog

        _CATALOG_CACHE.clear()
        skills_dir = self._skills_dir(tmp_path, "content_audit", "my_new_skill")
        catalog = build_domain_catalog(self._config_with(skills_dir))

        entry = catalog[0]
        assert entry["name"] == "docaudit"
        assert entry["description"] == "领域描述"
        assert {s["name"] for s in entry["skills"]} == {"content_audit", "my_new_skill"}
        by_name = {s["name"]: s["description"] for s in entry["skills"]}
        assert by_name["my_new_skill"] == "技能1描述"

    def test_catalog_cache_invalidates_on_dir_mtime_change(self, tmp_path):
        import os
        import time

        from courtier.agent.runtime.activation import _CATALOG_CACHE, build_domain_catalog

        _CATALOG_CACHE.clear()
        skills_dir = self._skills_dir(tmp_path, "existing_skill")
        cfg = self._config_with(skills_dir)
        first = build_domain_catalog(cfg)
        assert {s["name"] for s in first[0]["skills"]} == {"existing_skill"}

        # New skill created via the admin UI; bump the dir mtime so the
        # cache invalidates (write may land in the same mtime tick).
        (skills_dir / "brand_new.md").write_text(
            "---\nname: brand_new\ndescription: 新技能\n"
            "type: skill\nversion: '1.0'\nenabled: true\n---\n\n正文。",
            encoding="utf-8",
        )
        os.utime(skills_dir, (time.time() + 2, time.time() + 2))

        second = build_domain_catalog(cfg)
        assert {s["name"] for s in second[0]["skills"]} == {"existing_skill", "brand_new"}

    def test_catalog_cache_invalidates_on_inplace_edit(self, tmp_path):
        """The admin update path rewrites a skill file in place — only the
        file's mtime changes, never the directory's.  The catalog must still
        refresh (the bug this fixes: edits were invisible until restart)."""
        import os

        from courtier.agent.runtime.activation import _CATALOG_CACHE, build_domain_catalog

        _CATALOG_CACHE.clear()
        skills_dir = self._skills_dir(tmp_path, "my_skill")
        cfg = self._config_with(skills_dir)
        first = build_domain_catalog(cfg)
        assert first[0]["skills"][0]["description"] == "技能0描述"

        (skills_dir / "my_skill.md").write_text(
            "---\nname: my_skill\ndescription: 改后的描述\n"
            "type: skill\nversion: '1.0'\nenabled: true\n---\n\n正文 v2。",
            encoding="utf-8",
        )
        # Pin the dir mtime in the past to prove invalidation comes from
        # the file signature, not the directory.
        os.utime(skills_dir, (1_000_000_000, 1_000_000_000))

        second = build_domain_catalog(cfg)
        assert second[0]["skills"][0]["description"] == "改后的描述"

    def test_catalog_cache_invalidates_on_enabled_toggle(self, tmp_path):
        """Toggling enabled in place must immediately remove/add the skill
        from the catalog the model sees."""
        import os

        from courtier.agent.runtime.activation import _CATALOG_CACHE, build_domain_catalog

        _CATALOG_CACHE.clear()
        skills_dir = self._skills_dir(tmp_path, "my_skill")
        cfg = self._config_with(skills_dir)
        assert {s["name"] for s in build_domain_catalog(cfg)[0]["skills"]} == {"my_skill"}

        (skills_dir / "my_skill.md").write_text(
            "---\nname: my_skill\ndescription: 技能\n"
            "type: skill\nversion: '1.0'\nenabled: false\n---\n\n正文。",
            encoding="utf-8",
        )
        os.utime(skills_dir, (1_000_000_000, 1_000_000_000))
        assert build_domain_catalog(cfg)[0]["skills"] == []

        # Re-enable: the skill reappears.
        (skills_dir / "my_skill.md").write_text(
            "---\nname: my_skill\ndescription: 技能\n"
            "type: skill\nversion: '1.0'\nenabled: true\n---\n\n正文。",
            encoding="utf-8",
        )
        os.utime(skills_dir, (1_000_000_000, 1_000_000_000))
        assert {s["name"] for s in build_domain_catalog(cfg)[0]["skills"]} == {"my_skill"}


class TestActivateDomainToolCatalog:
    def test_description_embeds_skill_sublist(self):
        tool = ActivateDomainTool(
            [
                {
                    "name": "docaudit",
                    "description": "公文审计",
                    "skills": [{"name": "content_audit", "description": "内容审核"}],
                }
            ]
        )
        assert "领域技能：" in tool.description
        assert "content_audit：内容审核" in tool.description

    def test_description_without_skills_stays_compatible(self):
        tool = ActivateDomainTool([{"name": "docaudit", "description": "公文审计"}])
        assert "领域技能：" not in tool.description
        assert "docaudit" in tool.description


class TestActivatedSkillToolStreaming:
    """激活注入的 SkillTool 必须绑定当前 run 的流式回调与根预算锚点。

    域激活发生在 run() 的 callback sweep 之后，若不绑定，子代理事件
    不会转发到前端（界面上看不到子代理内部过程）。
    """

    @pytest.mark.asyncio
    async def test_activation_binds_streaming_callback_and_root_handle(self, tmp_path):
        agent, activator, _ = _build_harness(tmp_path)

        async def fake_callback(event):
            pass

        agent._subagent_event_callback = fake_callback
        agent._subagent_root_handle = object()

        await activator.activate("docaudit")

        tool = agent.tool_registry.get("format_audit")
        assert isinstance(tool, SkillTool)
        assert tool._on_subagent_event is fake_callback
        assert tool._parent_handle is agent._subagent_root_handle

    @pytest.mark.asyncio
    async def test_skill_tool_passes_callback_to_delegate(self, tmp_path, monkeypatch):
        """execute() 把绑定的回调透传给 AgentRuntime.delegate——事件到前端的关键一跳。"""
        import courtier.agent.runtime.runtime as runtime_mod

        agent, activator, _ = _build_harness(tmp_path)

        async def fake_callback(event):
            pass

        agent._subagent_event_callback = fake_callback
        await activator.activate("docaudit")

        captured: dict = {}

        async def fake_delegate(self, handle, **kwargs):
            captured["on_subagent_event"] = kwargs.get("on_subagent_event")
            from courtier.agent.runtime.result import ExecutionResult

            return ExecutionResult(
                success=True,
                actor_type="skill",
                actor_name=handle.agent_name,
                raw_data={"ok": True},
            )

        monkeypatch.setattr(runtime_mod.AgentRuntime, "delegate", fake_delegate)

        tool = agent.tool_registry.get("format_audit")

        result = await tool.execute(
            task="审核格式",
            file_path="/tmp/doc.pdf",
            mode="subagent",
            on_progress=lambda progress: None,
        )
        assert result.success is True
        assert captured.get("on_subagent_event") is fake_callback


class _ScriptedModelClient(MockModelClient):
    """Records messages per generate() call so tests can assert what the
    model actually saw on each turn of a run."""

    def __init__(self, tool_calls=None):
        super().__init__(tool_calls=tool_calls or [])
        self.captured: list[list[dict]] = []

    async def generate(self, messages, tools=None, **kwargs):
        self.captured.append(list(messages))
        return await super().generate(messages, tools=tools, **kwargs)


class TestMidRunActivationPromptRefresh:
    @pytest.mark.asyncio
    async def test_activation_rules_reach_the_model_next_turn(self, tmp_path):
        """Regression for sess_f575a957e19d.

        A fresh session activates the domain at turn 0 (mid-run).  The
        activation payload (workflow rules) written into the prompt pipeline
        must be materialized into the system message from the NEXT turn on —
        previously the run's system message was frozen at run() start, so the
        orchestrator executed the whole task without the domain's data
        fetching rules.
        """
        from courtier.agent.core.tool_call import ToolCall

        agent, _activator, _shared = _build_harness(tmp_path)
        client = _ScriptedModelClient(
            tool_calls=[
                ToolCall(id="call_1", name="activate_domain", arguments={"domain": "docaudit"})
            ]
        )
        agent.model = client

        result = await agent.run(task="审核这篇文档")
        assert result.content
        assert len(client.captured) >= 2

        first_sys = client.captured[0][0]
        assert first_sys["role"] == "system"
        # Pre-activation: no domain rules, no English fallback noise.
        assert "领域规则" not in first_sys["content"]
        assert "Workflow Rules" not in first_sys["content"]

        # From the turn after activate_domain, the payload is live.
        second_sys = client.captured[1][0]
        assert second_sys["role"] == "system"
        assert "领域规则（docaudit）" in second_sys["content"]

    @pytest.mark.asyncio
    async def test_dirty_flag_lifecycle(self, tmp_path):
        """mark_system_prompt_dirty() flags the agent; run() start consumes it."""
        agent, activator, _ = _build_harness(tmp_path)
        assert agent._system_prompt_dirty is False

        await activator.activate("docaudit")
        assert agent._system_prompt_dirty is True

        # A run start rebuilds the prompt (which already carries the overlay)
        # and clears the flag — no redundant refresh mid-run.
        client = _ScriptedModelClient()
        agent.model = client
        await agent.run(task="直接回答")
        assert agent._system_prompt_dirty is False
        assert "领域规则（docaudit）" in client.captured[0][0]["content"]
