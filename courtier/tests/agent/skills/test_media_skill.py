"""T10: MediaRef skill inputs — schema projection, data section, spawn wiring."""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from courtier.agent.core.content_parts import (
    AudioRef,
    ImageRef,
    MediaPart,
    MediaRef,
    VideoRef,
)


class _SkillInput:
    """Input-model stand-in with one media + one text field."""

    pass


def _make_input_model():
    from pydantic import BaseModel, Field

    class _Input(BaseModel):
        task: str = ""
        image: ImageRef = Field(description="待检查图片")
        note: str | None = None

    return _Input


class TestMediaRef:
    def test_accepts_plain_string(self):
        ref = ImageRef.model_validate("file_abc")
        assert ref.file_id == "file_abc"

    def test_accepts_dict(self):
        ref = VideoRef.model_validate({"file_id": "f9"})
        assert ref.file_id == "f9" and VideoRef.media_kind() == "video"

    def test_kinds(self):
        assert ImageRef.media_kind() == "image"
        assert AudioRef.media_kind() == "audio"
        assert VideoRef.media_kind() == "video"
        with pytest.raises(NotImplementedError):
            MediaRef.media_kind()

    def test_empty_string_accepted_as_file_id(self):
        # 空 file_id 由门控/物化层兜底（file 不存在 → 占位降级），schema 层不拦
        assert AudioRef.model_validate("").file_id == ""


class TestMediaFieldProjection:
    def _tool(self, input_model):
        from courtier.agent.tools.builtin.skill import SkillTool

        skill = SimpleNamespace(
            name="visual_inspection",
            display_name="图片视觉检查",
            description="检查图片",
            default_mode="subagent",
            input_model=input_model,
        )
        return SkillTool(skill=skill, runtime=None, prompt_engine=None)

    def test_media_field_projects_as_string(self):
        tool = self._tool(_make_input_model())
        prop = tool.parameters["properties"]["image"]
        assert prop["type"] == "string"
        assert "file_id" in prop["description"]

    def test_media_fields_detected(self):
        tool = self._tool(_make_input_model())
        assert tool._media_fields() == {"image": "image"}

    def test_data_section_renders_inline_marker(self):
        tool = self._tool(_make_input_model())
        validated = _make_input_model().model_validate(
            {"task": "t", "image": "file_1", "note": "备注"}
        )
        section = tool._render_data_section(validated)
        assert "[媒体附件已随消息内联: file_1]" in section
        assert "## note\n备注" in section

    def test_extract_media_parts(self):
        tool = self._tool(_make_input_model())
        validated = _make_input_model().model_validate({"task": "t", "image": "file_1"})
        assert tool._extract_media_parts(validated) == (
            MediaPart(kind="image", file_id="file_1", name="file_1"),
        )


class TestModalityHelpers:
    def test_declared_modalities_reach_through(self):
        from courtier.agent.tools.builtin.skill import _declared_modalities

        model = SimpleNamespace(
            _backend=SimpleNamespace(_declared_modalities=("vision",))
        )
        assert _declared_modalities(model) == ("vision",)

    def test_declared_modalities_missing_backend_is_empty(self):
        from courtier.agent.tools.builtin.skill import _declared_modalities

        assert _declared_modalities(SimpleNamespace()) == ()


class TestSpawnWiring:
    @pytest.mark.asyncio
    async def test_spawn_receives_media_parts(self):
        """Subagent-mode SkillTool forwards media parts to runtime.spawn."""
        from unittest.mock import AsyncMock, MagicMock

        from courtier.agent.tools.builtin.skill import SkillTool

        runtime = MagicMock()
        handle = MagicMock()
        runtime.spawn = MagicMock(return_value=handle)
        from courtier.agent.core.execution_result import ExecutionResult

        runtime.delegate = AsyncMock(
            return_value=ExecutionResult(
                success=True, actor_type="skill", actor_name="visual_inspection"
            )
        )
        runtime.terminate = AsyncMock()
        runtime.model = SimpleNamespace(
            _backend=SimpleNamespace(_declared_modalities=("vision",))
        )

        skill = SimpleNamespace(
            name="visual_inspection",
            display_name="图片视觉检查",
            description="检查图片",
            default_mode="subagent",
            input_model=_make_input_model(),
        )
        tool = SkillTool(skill=skill, runtime=runtime, prompt_engine=None)

        await tool.execute(
            on_progress=lambda _p: None,
            task="看图",
            mode="subagent",
            image="file_1",
        )
        kwargs = runtime.spawn.call_args.kwargs
        assert kwargs["media_parts"] == (
            MediaPart(kind="image", file_id="file_1", name="file_1"),
        )

    @pytest.mark.asyncio
    async def test_spawn_blocked_without_modality(self):
        from unittest.mock import AsyncMock, MagicMock

        from courtier.agent.tools.builtin.skill import SkillTool

        runtime = MagicMock()
        runtime.spawn = AsyncMock()
        runtime.model = SimpleNamespace(
            _backend=SimpleNamespace(_declared_modalities=())
        )

        skill = SimpleNamespace(
            name="visual_inspection",
            display_name="图片视觉检查",
            description="检查图片",
            default_mode="subagent",
            input_model=_make_input_model(),
        )
        tool = SkillTool(skill=skill, runtime=runtime, prompt_engine=None)

        result = await tool.execute(
            on_progress=lambda _p: None,
            task="看图",
            mode="subagent",
            image="file_1",
        )
        assert result.success is False
        assert "不支持" in (result.error or "")
        runtime.spawn.assert_not_called()


class TestRuntimeHandleMedia:
    def test_agent_handle_carries_media_parts(self):
        from courtier.agent.runtime.budget import AgentRuntimeBudget
        from courtier.agent.runtime.handle import AgentHandle

        budget = AgentRuntimeBudget(
            max_runtime_seconds=10,
            max_cumulative_runtime_seconds=10,
            max_turns=5,
            max_depth=1,
            remaining_total_spawns=1,
        )
        handle = AgentHandle.create(
            agent_name="visual_inspection",
            agent_type="skill",
            task="t",
            budget=budget,
            media_parts=(MediaPart(kind="image", file_id="f1", name="a.png"),),
        )
        assert handle.media_parts[0].file_id == "f1"


class TestOptionalMediaFields:
    """双模态技能：Optional[MediaRef] 字段识别与校验（图片+视频扩展）。"""

    @staticmethod
    def _model():
        from pydantic import BaseModel

        from courtier.agent.core.content_parts import VideoRef

        class DualInput(BaseModel):
            task: str = ""
            image: ImageRef | None = None
            video: VideoRef | None = None

        return DualInput

    def test_union_fields_detected(self):
        from courtier.agent.tools.builtin.skill import SkillTool

        skill = SimpleNamespace(
            name="visual_inspection", display_name="视觉检查",
            description="检查", default_mode="subagent",
            input_model=self._model(),
        )
        tool = SkillTool(skill=skill, runtime=None, prompt_engine=None)
        assert tool._media_fields() == {"image": "image", "video": "video"}

    def test_requires_one_medium(self):
        import sys
        from pathlib import Path

        domain_root = str(Path(__file__).resolve().parents[3] / "domains" / "docaudit")
        if domain_root not in sys.path:
            sys.path.insert(0, domain_root)
        from skills.schemas.visual_inspection import VisualInspectionInput

        with pytest.raises(ValidationError):
            VisualInspectionInput.model_validate({"task": "t"})
        # 官方 schema 的双字段同样可被识别为媒体字段
        from courtier.agent.tools.builtin.skill import SkillTool

        skill = SimpleNamespace(
            name="visual_inspection", display_name="视觉检查",
            description="检查", default_mode="subagent",
            input_model=VisualInspectionInput,
        )
        tool = SkillTool(skill=skill, runtime=None, prompt_engine=None)
        assert tool._media_fields() == {"image": "image", "video": "video"}

    def test_extract_video_only(self):
        from courtier.agent.tools.builtin.skill import SkillTool

        skill = SimpleNamespace(
            name="visual_inspection", display_name="视觉检查",
            description="检查", default_mode="subagent",
            input_model=self._model(),
        )
        tool = SkillTool(skill=skill, runtime=None, prompt_engine=None)
        validated = self._model().model_validate({"task": "t", "video": "file_v"})
        assert tool._extract_media_parts(validated) == (
            MediaPart(kind="video", file_id="file_v", name="file_v"),
        )

    def test_data_section_marks_filled_medium(self):
        from courtier.agent.tools.builtin.skill import SkillTool

        skill = SimpleNamespace(
            name="visual_inspection", display_name="视觉检查",
            description="检查", default_mode="subagent",
            input_model=self._model(),
        )
        tool = SkillTool(skill=skill, runtime=None, prompt_engine=None)
        validated = self._model().model_validate({"task": "t", "video": "file_v"})
        section = tool._render_data_section(validated)
        assert "## video" in section
        assert "[媒体附件已随消息内联: file_v]" in section
        assert "## image" not in section
