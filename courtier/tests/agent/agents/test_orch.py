"""Tests for OrchestratorAgent wiring of the Skill/SubAgent system."""

from __future__ import annotations

import pytest

from courtier.agent.agents.orch import OrchestratorAgent
from courtier.agent.runtime import AgentRuntime
from courtier.agent.skills import SkillRegistry
from courtier.agent.testing import MockModelClient
from courtier.agent.tools.builtin.skill import SkillTool
from courtier.agent.tools.registry import ToolRegistry


def _make_skill_registry(tmp_path) -> SkillRegistry:
    skill_dir = tmp_path / "skills"
    skill_dir.mkdir()
    (skill_dir / "dummy.md").write_text(
        "---\nname: dummy\ndescription: A dummy skill for testing\ntools: []\n---\n\n"
        "This skill does nothing useful.",
        encoding="utf-8",
    )
    registry = SkillRegistry(skill_dir)
    registry.scan()
    return registry


def _make_agent_runtime(skill_registry: SkillRegistry | None = None) -> AgentRuntime:
    return AgentRuntime(
        tool_registry=ToolRegistry(),
        model=MockModelClient(tool_calls=[]),
        skill_registry=skill_registry,
    )


class TestOrchestratorSkillIntegration:
    def test_skill_tool_registered_when_skill_registry_provided(self, tmp_path):
        """When a SkillRegistry is supplied, each skill is exposed as a tool."""
        skill_registry = _make_skill_registry(tmp_path)
        runtime = _make_agent_runtime(skill_registry)

        agent = OrchestratorAgent(
            model=MockModelClient(tool_calls=[]),
            skill_registry=skill_registry,
            agent_runtime=runtime,
        )

        skill_tools = [
            t for t in agent.tool_registry.list_tools() if isinstance(t, SkillTool)
        ]
        assert any(t.name == "dummy" for t in skill_tools)

    def test_skill_tool_absent_without_skill_registry(self):
        """Without a SkillRegistry, no SkillTool should be registered."""
        agent = OrchestratorAgent(model=MockModelClient(tool_calls=[]))

        skill_tools = [
            t for t in agent.tool_registry.list_tools() if isinstance(t, SkillTool)
        ]
        assert not skill_tools

    def test_skill_catalog_appears_in_role(self, tmp_path):
        """The orchestrator role should mention available skills to the LLM."""
        skill_registry = _make_skill_registry(tmp_path)
        runtime = _make_agent_runtime(skill_registry)

        agent = OrchestratorAgent(
            model=MockModelClient(tool_calls=[]),
            skill_registry=skill_registry,
            agent_runtime=runtime,
        )

        # Fallback (no PromptEngine): skills appear in the role string
        assert "dummy" in agent.role
        assert "Available Skills" in agent.role

    def test_workflow_rules_prevent_planning_paralysis(self, tmp_path):
        """The orchestrator system prompt must enforce single-skill dispatch.

        When PromptEngine is not provided, workflow rules are empty (fallback).
        Full workflow rules come from config/prompts/{locale}/orchestrator.yaml
        when PromptEngine is wired in.
        """
        skill_registry = _make_skill_registry(tmp_path)
        runtime = _make_agent_runtime(skill_registry)

        agent = OrchestratorAgent(
            model=MockModelClient(tool_calls=[]),
            skill_registry=skill_registry,
            agent_runtime=runtime,
        )

        prompt = agent.build_system_prompt()
        # Fallback: no workflow rules injected, but skills should be in the role
        assert "dummy" in prompt
        # Core identity should be present — rendered from the display name
        # (agent_name), not the internal class name.
        assert "Courtier Orchestrator" in prompt

    def test_skill_tool_exposes_file_path_parameter(self, tmp_path):
        """Skill tools must accept file_path so it can be propagated to sub-agents."""
        skill_registry = _make_skill_registry(tmp_path)
        runtime = _make_agent_runtime(skill_registry)

        agent = OrchestratorAgent(
            model=MockModelClient(tool_calls=[]),
            skill_registry=skill_registry,
            agent_runtime=runtime,
        )

        skill_tools = [
            t for t in agent.tool_registry.list_tools() if isinstance(t, SkillTool)
        ]
        assert skill_tools
        for tool in skill_tools:
            props = tool.parameters.get("properties", {})
            assert "file_path" in props, f"Skill {tool.name} missing file_path parameter"
            assert "task" in props

    @pytest.mark.asyncio
    async def test_run_requires_file_path_context(self):
        """OrchestratorAgent.run() still requires a file_path in context."""
        model = MockModelClient(tool_calls=[])
        agent = OrchestratorAgent(model=model)

        with pytest.raises(ValueError, match="file_path"):
            await agent.run(task="audit something")

    @pytest.mark.asyncio
    async def test_skill_tool_inline_mode_returns_tool_result(self, tmp_path):
        """mode="inline" returns ToolResult with short confirmation in data
        and full instructions in metadata.inline_instruction."""
        from courtier.agent.tools.protocol import ToolResult

        skill_registry = _make_skill_registry(tmp_path)
        runtime = _make_agent_runtime(skill_registry)
        skill_config = skill_registry.get("dummy")
        assert skill_config is not None

        tool = SkillTool(skill=skill_config, runtime=runtime)
        progress_messages: list[dict] = []

        def on_progress(msg: dict) -> None:
            progress_messages.append(msg)

        result = await tool.execute(
            on_progress=on_progress,
            task="test task",
            mode="inline",
        )

        assert isinstance(result, ToolResult)
        assert result.success is True
        assert result.metadata["mode"] == "inline"
        assert result.metadata["is_subagent_result"] is False
        # data is now a short confirmation, not the full instruction
        assert "dummy" in str(result.data) or "内联指令已加载" in str(result.data)
        # full instructions are in metadata.inline_instruction for injection as role=user
        inline = result.metadata.get("inline_instruction")
        assert inline is not None, "inline_instruction should be in metadata"
        assert "dummy" in str(inline)
        assert "test task" in str(inline)
        assert any("inline" in str(m) for m in progress_messages)

    @pytest.mark.asyncio
    async def test_skill_tool_mode_is_required_no_default(self, tmp_path):
        """mode parameter must be required with no default — LLM must choose."""
        skill_registry = _make_skill_registry(tmp_path)
        runtime = _make_agent_runtime(skill_registry)
        skill_config = skill_registry.get("dummy")
        assert skill_config is not None
        assert skill_config.default_mode == ""

        tool = SkillTool(skill=skill_config, runtime=runtime)
        # mode parameter should have NO default and be required
        assert "default" not in tool.parameters["properties"]["mode"]
        assert "mode" in tool.parameters["required"]

    def test_skill_tool_parameters_include_mode(self, tmp_path):
        """SkillTool parameters must expose the mode enum (required, no default)."""
        skill_registry = _make_skill_registry(tmp_path)
        runtime = _make_agent_runtime(skill_registry)
        skill_config = skill_registry.get("dummy")
        assert skill_config is not None

        tool = SkillTool(skill=skill_config, runtime=runtime)
        mode_param = tool.parameters["properties"]["mode"]
        assert mode_param["type"] == "string"
        assert set(mode_param["enum"]) == {"subagent", "inline"}
        assert "default" not in mode_param
        assert "mode" in tool.parameters["required"]
