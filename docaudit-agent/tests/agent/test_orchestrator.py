"""Tests for OrchestratorAgent with AgentRuntime-backed Skill dispatch."""

from unittest.mock import MagicMock

import pytest

from courtier.agent.core.model import ToolCall
from courtier.agent.runtime import AgentRuntime
from courtier.agent.skills import SkillRegistry
from courtier.agent.testing import MockModelClient
from courtier.agent.tools.builtin.skill import SkillTool
from courtier.agent.tools.registry import ToolRegistry


def _make_mock_plugin_system():
    """Build a mock plugin system (plugin agents are no longer used)."""
    return MagicMock()


def _make_skill_registry(tmp_path, skills=None):
    """Build a SkillRegistry pointing at a temp dir with optional skill files."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    if skills is None:
        skills = [
            {
                "name": "dummy",
                "description": "A dummy skill for testing",
                "body": "This skill does nothing useful.",
            }
        ]
    for skill in skills:
        (skills_dir / f"{skill['name']}.md").write_text(
            f"---\n"
            f"name: {skill['name']}\n"
            f"description: {skill['description']}\n"
            f"tools: []\n"
            f"---\n\n"
            f"{skill['body']}",
            encoding="utf-8",
        )
    reg = SkillRegistry(skills_dir)
    reg.scan()
    return reg


def _make_agent_runtime(model, skill_registry=None):
    """Build a minimal AgentRuntime for tests."""
    return AgentRuntime(
        tool_registry=ToolRegistry(),
        model=model,
        skill_registry=skill_registry,
    )


class TestOrchestratorAgent:
    def test_orchestrator_extends_agent_base(self):
        from courtier.agent.agents.base import Agent

        orch = self._make_orchestrator()
        assert isinstance(orch, Agent)
        assert orch.name == "OrchestratorAgent"

    def test_skill_tools_registered_when_skill_registry_provided(self, tmp_path):
        """Each enabled skill is exposed as a tool when a SkillRegistry is supplied."""
        plugin_system = _make_mock_plugin_system()
        skill_registry = _make_skill_registry(tmp_path)
        model = MockModelClient(tool_calls=[])
        runtime = _make_agent_runtime(model, skill_registry)

        from courtier.agent.agents.orch import OrchestratorAgent

        orch = OrchestratorAgent(
            model=model,
            plugin_system=plugin_system,
            skill_registry=skill_registry,
            agent_runtime=runtime,
        )
        skill_tools = [
            t for t in orch.tool_registry.list_tools() if isinstance(t, SkillTool)
        ]
        assert len(skill_tools) > 0
        assert any(t.name == "dummy" for t in skill_tools)

    @pytest.mark.asyncio
    async def test_run_dispatches_skill(self, tmp_path):
        """Orchestrator dispatches to a skill tool for audit work."""
        plugin_system = _make_mock_plugin_system()
        skill_registry = _make_skill_registry(
            tmp_path,
            skills=[
                {
                    "name": "format_audit",
                    "description": "格式审核",
                    "body": "格式审核 Skill",
                }
            ],
        )

        model = MockModelClient(
            tool_calls=[
                ToolCall(
                    id="c1",
                    name="format_audit",
                    arguments={"task": "审核格式", "mode": "subagent"},
                ),
            ]
        )
        runtime = _make_agent_runtime(model, skill_registry)

        from courtier.agent.agents.orch import OrchestratorAgent

        orch = OrchestratorAgent(
            model=model,
            plugin_system=plugin_system,
            skill_registry=skill_registry,
            agent_runtime=runtime,
        )
        result = await orch.run(
            task="Audit /path/to/doc.pdf",
            context={"file_path": "/path/to/doc.pdf"},
        )
        assert result.status == "completed"

    @pytest.mark.asyncio
    async def test_run_without_file_path_raises(self):
        orch = self._make_orchestrator()
        with pytest.raises(ValueError, match="file_path"):
            await orch.run(task="Audit something")

    def test_all_expected_tools_present(self, tmp_path):
        """Orchestrator should always expose built-in tools."""
        plugin_system = _make_mock_plugin_system()
        skill_registry = _make_skill_registry(tmp_path)
        model = MockModelClient(tool_calls=[])
        runtime = _make_agent_runtime(model, skill_registry)

        from courtier.agent.agents.orch import OrchestratorAgent

        orch = OrchestratorAgent(
            model=model,
            plugin_system=plugin_system,
            skill_registry=skill_registry,
            agent_runtime=runtime,
        )
        tool_names = set(t.name for t in orch.tool_registry.list_tools())
        expected_subset = {
            "list_artifacts",
            "get_artifact",
        }
        assert expected_subset <= tool_names

    @pytest.mark.asyncio
    async def test_pipeline_collects_audit_results(self, tmp_path):
        from courtier.agent.agents.orch import OrchestratorAgent

        plugin_system = _make_mock_plugin_system()
        skill_registry = _make_skill_registry(
            tmp_path,
            skills=[
                {
                    "name": "format_audit",
                    "description": "格式审核",
                    "body": "格式审核 Skill",
                }
            ],
        )

        model = MockModelClient(
            tool_calls=[
                ToolCall(
                    id="c1",
                    name="format_audit",
                    arguments={"task": "审核格式", "mode": "subagent"},
                ),
            ]
        )
        runtime = _make_agent_runtime(model, skill_registry)

        orch = OrchestratorAgent(
            model=model,
            plugin_system=plugin_system,
            skill_registry=skill_registry,
            agent_runtime=runtime,
        )
        result = await orch.run(
            task="Audit",
            context={"file_path": "/path/to/doc.pdf"},
        )

        assert result.status == "completed"
        assert isinstance(orch.audit_results, dict)
        assert "format_audit" in orch.audit_results

    def _make_orchestrator(self, tool_calls=None, plugin_system=None, tmp_path=None):
        """Build OrchestratorAgent without a skill registry."""
        from courtier.agent.agents.orch import OrchestratorAgent

        model = MockModelClient(tool_calls=tool_calls or [])
        return OrchestratorAgent(
            model=model,
            plugin_system=plugin_system,
        )
