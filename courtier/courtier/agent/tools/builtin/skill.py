"""SkillTool — dispatches a registered skill through AgentRuntime."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from ....prompts.errors import render_error
from ...artifacts.models import RuntimePolicy
from ...core.execution_result import ExecutionResult
from ...skills.config import SkillConfig
from ...tools.protocol import OnToolProgress, ToolResult
from ...tools.summary import ToolSummary

if TYPE_CHECKING:
    from ...runtime import AgentRuntime
    from ...runtime.handle import AgentHandle

logger = logging.getLogger(__name__)

_OnSubagentEvent = Callable[..., Awaitable[None]]


class SkillTool:
    """A callable tool that runs a Skill via AgentRuntime.

    Each Skill is exposed to the orchestrator LLM as a tool named after the
    skill.  Executing the tool spawns a sub-agent, delegates the task, and
    returns the resulting ``ExecutionResult``.
    """

    name: str
    skill: str
    description: str
    parameters: dict[str, Any]
    # Prevent infinite loops: a parent agent calling the same skill
    # repeatedly (e.g. because sub-agent results are summarised away)
    # will be stopped after 3 consecutive calls or 10 total calls.
    runtime_policy = RuntimePolicy(max_calls=10, max_consecutive=3)
    # SkillTool enforces its own deadline via the AgentRuntime budget;
    # the registry-level wrapper stays off to avoid a double timeout.
    execution_timeout = 0

    def __init__(
        self,
        skill: SkillConfig,
        runtime: AgentRuntime,
        output_artifact_type: str | None = None,
        prompt_engine: Any | None = None,
    ) -> None:
        self.name = skill.name
        self.skill = skill.name
        self.display_name: str | None = skill.display_name or None
        self.output_artifact_type = output_artifact_type
        self.description = skill.description or f"执行 Skill: {skill.name}"
        self.default_mode = getattr(skill, "default_mode", "subagent")
        self.parameters = {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "要交给 Skill 执行的具体任务描述",
                },
                "file_path": {
                    "type": "string",
                    "description": "待审核文档的绝对路径（从 Orchestrator 上下文透传）",
                },
                "ref_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "可选的缓存引用 ID 列表",
                },
                "mode": {
                    "type": "string",
                    "enum": ["subagent", "inline"],
                    "description": (
                        "执行模式（必选，无默认值，你必须主动选择）："
                        "subagent=启动独立子代理异步执行（适合复杂/耗时任务），"
                        "inline=返回 Skill 指令由当前代理直接执行（适合简单/快速任务）"
                    ),
                },
            },
            "required": ["task", "mode"],
        }
        self._runtime = runtime
        self._skill = skill
        self._input_model = skill.input_model
        self._prompt_engine = prompt_engine
        if self._input_model is not None:
            # Structured data fields (e.g. document) join the tool schema so
            # the orchestrator can pass $ref references directly into them.
            self._merge_typed_parameters()
        self._on_subagent_event: _OnSubagentEvent | None = None
        self._parent_handle: AgentHandle | None = None

    def _merge_typed_parameters(self) -> None:
        """Project the skill's typed data fields onto the tool parameter schema.

        Only subclass-declared fields participate (framework plumbing like
        task/ref_ids stays out); Field descriptions carry the $ref usage
        guidance to the model.
        """
        from ...agents.subagent.base import data_field_names

        schema = self._input_model.model_json_schema()
        props = self.parameters["properties"]
        for name in sorted(data_field_names(self._input_model)):
            if name in props:
                raise ValueError(
                    f"Skill {self.name}: input_model field {name!r} conflicts "
                    f"with a built-in SkillTool parameter"
                )
            sub_schema = dict(schema.get("properties", {}).get(name, {}))
            sub_schema.pop("title", None)
            props[name] = sub_schema

    def _render_data_section(self, validated: Any) -> str:
        """Assemble validated data fields into the task's「# 输入数据」block.

        None fields are skipped entirely; strings pass through as-is (this is
        where a resolved $ref arrives as full document text); everything else
        is serialized as JSON.
        """
        from ...agents.subagent.base import data_field_names

        parts: list[str] = []
        for name in sorted(data_field_names(self._input_model)):
            value = getattr(validated, name)
            if value is None:
                continue
            if not isinstance(value, str):
                value = json.dumps(value, ensure_ascii=False, default=str)
            parts.append(f"## {name}\n{value}")
        return "\n".join(parts)

    def _render_validation_error(self, exc: Any) -> str:
        """Render a field-level validation report via errors.skill_input_validation."""
        from ....prompts.engine import PromptEngine

        engine = self._prompt_engine or PromptEngine()
        lines = []
        for err in exc.errors(include_url=False):
            loc = ".".join(str(p) for p in err.get("loc", ())) or "(root)"
            got = repr(err.get("input"))
            if len(got) > 120:
                got = got[:117] + "..."
            lines.append(f"- {loc}: {err.get('msg')}; got={got}")
        return engine.render(
            "errors.skill_input_validation",
            skill_name=self.name,
            error_details="\n".join(lines),
        )

    def set_callbacks(
        self,
        *,
        on_subagent_event: _OnSubagentEvent | None = None,
    ) -> None:
        """Attach sub-agent event callback for streaming."""
        self._on_subagent_event = on_subagent_event

    def set_parent_handle(self, handle: AgentHandle | None) -> None:
        """Attach the parent runtime handle for budget/cycle tracking."""
        self._parent_handle = handle

    async def execute(
        self,
        *,
        on_progress: OnToolProgress,
        **kwargs: Any,
    ) -> ToolResult | ExecutionResult:
        """Spawn the skill agent, delegate the task, and return the result."""
        task: str = kwargs.pop("task", "")
        file_path: str | None = kwargs.pop("file_path", None)
        ref_ids: list[str] | None = kwargs.pop("ref_ids", None)
        mode: str = kwargs.pop("mode", "")
        if not mode:
            return ToolResult(
                success=False,
                error=render_error(
                    "errors.skill_missing_mode",
                    skill_name=self.name,
                    engine=self._prompt_engine,
                ),
            )

        if not task:
            return ToolResult(
                success=False,
                error=render_error(
                    "errors.tool_missing_param",
                    tool_name=self.name,
                    param_name="task",
                    engine=self._prompt_engine,
                ),
            )

        # Typed data fields (e.g. document) — validated against the skill's
        # input model, then assembled into a "# 输入数据" section appended to
        # the task so the sub-agent receives the resolved values.
        data_fields = {
            k: v
            for k, v in kwargs.items()
            if k not in ("context_manager", "artifact_store", "audit_logger")
        }
        data_section = ""
        if self._input_model is not None:
            try:
                # The base model's task field is satisfied from the task
                # parameter — validation covers the data payload only.
                validated = self._input_model.model_validate({"task": task, **data_fields})
            except ValidationError as exc:
                return ToolResult(success=False, error=self._render_validation_error(exc))
            data_section = self._render_data_section(validated)
        elif data_fields:
            # No schema declared: unknown extra arguments would silently vanish.
            logger.warning(
                "Skill %s received unexpected arguments (no input_model): %s",
                self.name,
                sorted(data_fields),
            )
        final_task = f"{task}\n\n# 输入数据\n{data_section}" if data_section else task

        # --- inline mode: return skill instructions for direct execution ---
        if mode == "inline":
            return await self._execute_inline(
                task=final_task,
                file_path=file_path,
                on_progress=on_progress,
            )

        # --- subagent mode (original path) ---
        on_progress(
            {
                "status": "running",
                "message": f"启动 Skill {self.name}...",
                "detail": None,
            }
        )

        if self._runtime is None:
            return ToolResult(
                success=False,
                error=render_error(
                    "errors.skill_runtime_unavailable",
                    skill_name=self.name,
                    engine=self._prompt_engine,
                ),
            )

        context: dict[str, str] | None = None
        if file_path:
            context = {"file_path": file_path}

        try:
            handle = self._runtime.spawn(
                name=self.name,
                task=final_task,
                parent_handle=self._parent_handle,
                ref_ids=ref_ids or [],
                context=context,
            )
        except (ValueError, RuntimeError) as exc:
            logger.exception("Failed to spawn skill %s", self.name)
            return ToolResult(
                success=False,
                error=render_error(
                    "errors.skill_spawn_failed",
                    skill_name=self.name,
                    error=str(exc),
                    engine=self._prompt_engine,
                ),
            )

        try:
            result = await self._runtime.delegate(
                handle,
                context_manager=kwargs.get("context_manager"),
                artifact_store=kwargs.get("artifact_store"),
                audit_logger=kwargs.get("audit_logger"),
                on_subagent_event=self._on_subagent_event,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Skill %s delegation failed", self.name)
            return ToolResult(
                success=False,
                error=render_error(
                    "errors.skill_failed",
                    skill_name=self.name,
                    error=str(exc),
                    engine=self._prompt_engine,
                ),
            )
        finally:
            await self._runtime.terminate(handle)

        on_progress(
            {
                "status": "done",
                "message": f"Skill {self.name} 完成",
                "detail": None,
            }
        )

        metadata = {
            **result.metadata,
            "skill": self.name,
            "is_subagent_result": True,
            # Classify as a sub-agent dispatch so the API/SSE layer and the
            # frontend merge this tool record into the sub-agent tree node
            # instead of rendering a duplicate standalone tool card.  The
            # orchestrator's root handle is a bookkeeping artifact, not a
            # sub-agent, so top-level dispatches stay "parent" scope.
            "call_kind": "subagent_run",
            "call_scope": (
                "subagent"
                if (
                    self._parent_handle is not None
                    and getattr(self._parent_handle, "agent_type", None) != "orchestrator"
                )
                else "parent"
            ),
            "subagent_name": self.name,
            "parent_subagent_name": (
                self._parent_handle.agent_name if self._parent_handle is not None else None
            ),
            "handle_id": handle.handle_id,
            "parent_handle_id": handle.parent_handle_id,
        }
        if not result.success:
            return ToolResult(
                success=False,
                error=result.error
                or render_error(
                    "errors.result_unknown_error",
                    actor_name=self.name,
                    engine=self._prompt_engine,
                ),
                metadata=metadata,
            )

        # Pass the sub-agent's ExecutionResult straight through with the
        # skill-routing metadata attached — no ToolResult re-wrapping, the
        # registry's single-track entry handles ExecutionResult directly.
        return replace(result, metadata=metadata)

    async def _execute_inline(
        self,
        *,
        task: str,
        file_path: str | None,
        on_progress: OnToolProgress,
    ) -> ToolResult:
        """Inline execution: return skill instructions for direct execution.

        Instead of spawning a sub-agent, inject the skill's system prompt and
        task into the current agent's context, letting the orchestrator execute
        the work directly with its own tools.
        """
        # Check whether the skill declares required tools that are unavailable
        # in the runtime.  If they are missing the inline instructions will be
        # useless — the calling agent won't be able to follow them.  Fail fast
        # with a clear suggestion to use subagent mode instead.
        required_tools = getattr(self._skill, "tools", ()) or ()
        if required_tools:
            available = set()
            if self._runtime is not None and hasattr(self._runtime, "tool_registry"):
                available = {t.name for t in self._runtime.tool_registry.list_tools()}
            missing = set(required_tools) - available
            if missing:
                return ToolResult(
                    success=False,
                    error=render_error(
                        "errors.skill_inline_missing_tools",
                        skill_name=self.name,
                        missing_tools=sorted(missing),
                        engine=self._prompt_engine,
                    ),
                )

        on_progress(
            {
                "status": "running",
                "message": f"加载 Skill {self.name} 指令（inline 模式）...",
                "detail": None,
            }
        )

        context_notes = ""
        if file_path:
            context_notes = f"\n\n> 文档路径：`{file_path}`"

        # Build a structured inline instruction that guides the current agent
        # to perform the skill's work without a sub-agent round-trip.
        inline_instruction = (
            f"## 任务：{self.name}\n\n"
            f"{self._skill.system_prompt}\n\n"
            f"---\n\n"
            f"## 待执行任务\n\n"
            f"{task}"
            f"{context_notes}\n\n"
            f"---\n\n"
            f"> 请使用你可用的工具直接执行上述任务，无需再次调用 Skill 工具。"
            f"完成后输出结构化结果。"
        )

        on_progress(
            {
                "status": "done",
                "message": f"Skill {self.name} 指令已加载（inline 模式）",
                "detail": None,
            }
        )

        # Return a short confirmation as the tool result (role=tool) so the
        # model knows the load succeeded, and place the full instructions in
        # metadata.  The loop's add_observation() will inject the instructions
        # as a role=user message — the model sees them as a task to execute,
        # not as a completed tool output.
        confirmation = (
            f"Skill「{self.name}」内联指令已加载。"
            f"请按后续用户消息中的指令执行 {self._skill.description or self.name}。"
        )

        return ToolResult(
            success=True,
            data=confirmation,
            metadata={
                "skill": self.name,
                "mode": "inline",
                "is_subagent_result": False,
                "inline_instruction": inline_instruction,
            },
        )

    def summarize(self, result: Any) -> ToolSummary:
        """Produce a display summary for a skill result."""
        if isinstance(result, ExecutionResult):
            if not result.success:
                return ToolSummary(
                    status="err",
                    chips=[("error", result.error or "未知错误")],
                    issue_counts=None,
                    detail=result.error,
                )
            return ToolSummary(
                status="ok",
                chips=[("skill", self.name)],
                issue_counts=result.metadata.get("issue_counts"),
                detail=result.raw_data,
            )

        return ToolSummary(
            status="err",
            chips=[("error", f"非预期结果类型: {type(result).__name__}")],
            issue_counts=None,
            detail=None,
        )
