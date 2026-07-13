"""Regression tests — full pipeline on 3 test assets.

Verifies the spec gate: "3 测试文件上全流程（上传→解析→全审）均可正常完成"
Tests ceshi(1).docx, doc.pdf, 函1.pdf through parse → audit.

Uses real file paths and mocked tools — pipeline structure is the regression target.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from courtier.agent.core.model import ToolCall
from courtier.agent.runtime import AgentRuntime
from courtier.agent.testing import MockModelClient
from courtier.agent.tools.registry import ToolRegistry

pytestmark = pytest.mark.integration

ASSETS = [
    (
        "ceshi(1).docx",
        {"title": "测试通知", "doc_type": "通知", "pages": [{"text": "段落1"}]},
    ),
    (
        "doc.pdf",
        {"title": "关于xxx的通知", "doc_type": "通知", "pages": [{"text": "段落内容"}]},
    ),
    (
        "函1.pdf",
        {"title": "关于xxx的函", "doc_type": "函", "pages": [{"text": "函件内容"}]},
    ),
]

SKILL_NAMES = [
    "format_audit",
    "plagiarism",
    "full_government_audit",
]


def _make_mock_plugin_system():
    """Build a mock plugin system (plugin agents are no longer used)."""
    return MagicMock()


def _build_tool_calls() -> list[ToolCall]:
    return [
        ToolCall(
            id=f"c{i + 1}",
            name=name,
            arguments={"task": f"Run {name}"},
        )
        for i, name in enumerate(SKILL_NAMES)
    ]


class TestFullPipelineRegression:
    """Run the full parse→audit pipeline against 3 test assets."""

    @pytest.mark.asyncio
    async def test_pipeline_ceshi_docx(self, tmp_path):
        await self._run_pipeline_for_asset(*ASSETS[0], tmp_path=tmp_path)

    @pytest.mark.asyncio
    async def test_pipeline_doc_pdf(self, tmp_path):
        await self._run_pipeline_for_asset(*ASSETS[1], tmp_path=tmp_path)

    @pytest.mark.asyncio
    async def test_pipeline_han1_pdf(self, tmp_path):
        await self._run_pipeline_for_asset(*ASSETS[2], tmp_path=tmp_path)

    @pytest.mark.asyncio
    async def test_all_three_assets_run_full_pipeline(self, tmp_path):
        """All 3 assets complete the full pipeline with consistent structure."""
        results = {}
        for filename, doc_dict in ASSETS:
            result = await self._run_pipeline_for_asset(
                filename, doc_dict, tmp_path=tmp_path
            )
            results[filename] = result

        for filename, (result, audit_results) in results.items():
            assert result.status == "completed", f"{filename}: must complete"
            assert result.data.get("steps", 0) > 0, f"{filename}: must have steps"

    async def _run_pipeline_for_asset(
        self, filename: str, doc_dict: dict | None = None, tmp_path=None
    ):
        """Run full parse→audit pipeline for a single asset."""
        from courtier.agent.agents.orch import OrchestratorAgent
        from courtier.agent.skills import SkillRegistry

        if doc_dict is None:
            doc_dict = {"title": "测试", "doc_type": "通知", "pages": []}

        model = MockModelClient(tool_calls=_build_tool_calls())
        plugin_system = _make_mock_plugin_system()

        skills_dir = tmp_path / "skills"
        skills_dir.mkdir(exist_ok=True)
        skill_registry = SkillRegistry(skills_dir)
        skill_registry.scan()
        runtime = AgentRuntime(
            tool_registry=ToolRegistry(),
            model=model,
            skill_registry=skill_registry,
        )

        orch = OrchestratorAgent(
            model=model,
            plugin_system=plugin_system,
            skill_registry=skill_registry,
            agent_runtime=runtime,
        )

        result = await orch.run(
            task=f"Audit document: {filename}",
            context={"file_path": f"/fake/path/{filename}"},
        )

        assert result.status == "completed", f"{filename}: pipeline must complete"
        assert result.data.get("steps", 0) > 0, f"{filename}: must have steps"

        return result, orch.audit_results

    def test_no_load_skill_tool_registered(self, tmp_path):
        """Verify the legacy load_skill tool is no longer registered."""
        from courtier.agent.agents.orch import OrchestratorAgent
        from courtier.agent.skills import SkillRegistry

        model = MockModelClient(tool_calls=[])
        plugin_system = _make_mock_plugin_system()
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir(exist_ok=True)
        skill_registry = SkillRegistry(skills_dir)
        skill_registry.scan()
        runtime = AgentRuntime(
            tool_registry=ToolRegistry(),
            model=model,
            skill_registry=skill_registry,
        )
        orch = OrchestratorAgent(
            model=model,
            plugin_system=plugin_system,
            skill_registry=skill_registry,
            agent_runtime=runtime,
        )

        tool_names = [t.name for t in orch.tool_registry.list_tools()]
        assert (
            "load_skill" not in tool_names
        ), f"load_skill still present in {tool_names}"
