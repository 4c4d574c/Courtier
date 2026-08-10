"""End-to-end integration test for the Skill + SubAgent + Orchestrator path.

Exercises the real wiring (no mocks beyond the model): OrchestratorAgent builds a
SkillTool from a live SkillRegistry, the model emits a ``echo`` skill tool call,
and the skill tool spawns a sub-agent through AgentRuntime that resolves its
required tool from the shared ToolRegistry.
"""

import pytest

from courtier.agent.agents.orch import OrchestratorAgent
from courtier.agent.core.model import ToolCall
from courtier.agent.runtime import AgentRuntime
from courtier.agent.skills import SkillRegistry
from courtier.agent.testing import MockModelClient
from courtier.agent.tools.builtin.echo import EchoTool
from courtier.agent.tools.registry import ToolRegistry


@pytest.fixture
def skill_registry(tmp_path):
    """A registry with a single `echo` skill that requires the `echo` tool."""
    skill_dir = tmp_path / "skills"
    skill_dir.mkdir()
    (skill_dir / "echo_skill.md").write_text(
        "---\nname: echo_skill\ndescription: 回显 Skill\ntools:\n  - echo\n---\n\n"
        "调用 echo 工具返回输入文本。",
        encoding="utf-8",
    )
    reg = SkillRegistry(skill_dir)
    reg.scan()
    return reg


@pytest.fixture
def tool_registry():
    reg = ToolRegistry()
    reg.register(EchoTool())
    return reg


@pytest.mark.asyncio
async def test_orchestrator_can_run_skill(skill_registry, tool_registry):
    """The orchestrator runs a skill tool call end-to-end and completes."""
    model = MockModelClient(
        tool_calls=[
            ToolCall(id="1", name="echo_skill", arguments={"task": "hi", "mode": "subagent"})
        ]
    )
    runtime = AgentRuntime(
        tool_registry=tool_registry,
        model=model,
        skill_registry=skill_registry,
    )

    agent = OrchestratorAgent(
        model=model,
        tool_registry=tool_registry,
        skill_registry=skill_registry,
        agent_runtime=runtime,
    )

    result = await agent.run("use echo skill", context={"file_path": "/tmp/test.txt"})

    assert result.status == "completed"
    # The echo skill tool executed and produced a sub-agent result.
    skill_results = [tr for tr in result.tool_results if tr.metadata.get("skill") == "echo_skill"]
    assert skill_results, "expected a skill result tagged with skill=echo_skill"
    assert skill_results[0].success is True
    assert skill_results[0].metadata.get("is_subagent_result") is True
    # Dispatch records are classified so the UI merges them into the
    # sub-agent tree node instead of rendering a duplicate tool card.
    assert skill_results[0].metadata.get("call_kind") == "subagent_run"
    assert skill_results[0].metadata.get("call_scope") == "parent"
    assert skill_results[0].metadata.get("subagent_name") == "echo_skill"
    assert skill_results[0].metadata.get("handle_id")


@pytest.mark.asyncio
async def test_orchestrator_load_unknown_skill_completes_with_failure(
    skill_registry, tool_registry
):
    """An unknown tool name fails the tool but the orchestrator loop still completes."""
    model = MockModelClient(
        tool_calls=[
            ToolCall(
                id="1",
                name="does_not_exist",
                arguments={"task": "hi"},
            )
        ]
    )
    runtime = AgentRuntime(
        tool_registry=tool_registry,
        model=model,
        skill_registry=skill_registry,
    )

    agent = OrchestratorAgent(
        model=model,
        tool_registry=tool_registry,
        skill_registry=skill_registry,
        agent_runtime=runtime,
    )

    result = await agent.run("use missing skill", context={"file_path": "/tmp/test.txt"})

    assert result.status == "completed"
    failed = [tr for tr in result.tool_results if not tr.success]
    assert failed, "expected a failed tool result"
    # The recoverable guidance error names the missing tool and lists
    # what IS available so the model can self-correct.
    assert "does_not_exist" in (failed[0].error or "")
    assert "未注册" in (failed[0].error or "")
    assert "echo_skill" in (failed[0].error or "")
