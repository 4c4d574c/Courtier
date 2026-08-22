"""OrchestratorAgent — composes domain agents for the full audit pipeline."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from courtier.prompts.engine import PromptEngine

from ..core.model import ModelClient
from ..core.state import AgentState
from ..hooks.chain import HookChain
from ..permissions.gate import PermissionGate
from ..skills import SkillRegistry
from ..tools.builtin.get_artifact import GetArtifactTool
from ..tools.builtin.list_artifacts import ListArtifactsTool
from ..tools.builtin.skill import SkillTool
from ..tools.protocol import ToolProgress, ToolProtocol
from .base import Agent, AgentResult

if TYPE_CHECKING:
    from ..runtime import AgentRuntime
    from ..runtime.handle import AgentHandle

logger = logging.getLogger(__name__)


class OrchestratorAgent(Agent):
    """Composes domain agents for the full audit pipeline.

    Flow: LLM-driven planning with atomic tools (parse, list_artifacts, etc.).
    Domain skills are dispatched as first-class sub-agents through the
    AgentRuntime rather than through a ``load_skill`` tool adapter.
    """

    def __init__(
        self,
        model: ModelClient | None = None,
        hooks: HookChain | None = None,
        permissions: PermissionGate | None = None,
        selected_auditors: list[str] | None = None,
        plugin_system: Any = None,
        tool_registry: Any = None,
        skill_registry: SkillRegistry | None = None,
        agent_runtime: AgentRuntime | None = None,
        courtier_md_content: str | None = None,
        prompt_engine: PromptEngine | None = None,
        agent_name: str = "Courtier Orchestrator",
        first_required_tool: str | None = None,
        extra_tools: list[ToolProtocol] | None = None,
        tool_filter: Callable[[Any], bool] | None = None,
    ) -> None:
        self._audit_results: dict[str, Any] = {}
        self._agent_runtime = agent_runtime
        self._skill_callback_lock = asyncio.Lock()
        # Current-run sub-agent streaming callback and root budget handle,
        # exposed so SkillTools injected by a mid-run domain activation
        # (which the run()-start callback sweep cannot see) can bind them.
        self._subagent_event_callback: Callable[..., Awaitable[None]] | None = None
        self._subagent_root_handle: Any | None = None

        if model is None:
            raise ValueError(
                "OrchestratorAgent requires a ModelClient instance. "
                "For testing, pass MockModelClient(tool_calls=[...]) explicitly."
            )
        _model = model

        tools: list[ToolProtocol] = [
            ListArtifactsTool(),
            GetArtifactTool(),
        ]
        if extra_tools:
            tools.extend(extra_tools)

        skill_catalog = ""
        if skill_registry is not None:
            if agent_runtime is None:
                raise ValueError(
                    "OrchestratorAgent requires an AgentRuntime when a SkillRegistry is provided."
                )
            skill_tools = self._build_skill_tools(skill_registry, agent_runtime, prompt_engine)
            tools.extend(skill_tools)
            skill_catalog = skill_registry.build_catalog()

        # Build skill list for template rendering
        skill_list = []
        if skill_registry is not None:
            for skill in skill_registry.list_enabled():
                skill_list.append(
                    {
                        "name": skill.name,
                        "description": skill.description,
                    }
                )

        if prompt_engine is not None:
            role = prompt_engine.render(
                "orchestrator.system_prompt",
                agent_name=agent_name,
                available_skills=skill_list,
            )
        else:
            # Fallback for tests that don't provide PromptEngine
            role = (
                f"You are {agent_name}. "
                "Your job is to understand user needs and call appropriate Skills."
            )
            if skill_catalog:
                role += f"\n\nAvailable Skills:\n{skill_catalog}"

        # No construction-time workflow_rules render: orchestrator.workflow_rules
        # is a domain-owned activation payload. DomainActivator.activate() is its
        # sole writer — rendering it here would either bake the English fallback
        # into every session or leak not-yet-activated domains' rules (referencing
        # gated tools) into sessions that never activated them.
        super().__init__(
            name="OrchestratorAgent",
            role=role,
            tools=tools,
            model=_model,
            hooks=hooks,
            permissions=permissions,
            tool_registry=tool_registry,
            courtier_md_content=courtier_md_content,
            prompt_engine=prompt_engine,
            agent_name=agent_name,
            first_required_tool=first_required_tool,
            tool_filter=tool_filter,
        )

    @staticmethod
    def _build_skill_tools(
        skill_registry: SkillRegistry,
        runtime: AgentRuntime,
        prompt_engine: Any | None = None,
    ) -> list[SkillTool]:
        """Build a SkillTool for every enabled skill in the registry."""
        return [
            SkillTool(
                skill=skill,
                runtime=runtime,
                output_artifact_type=skill.output_artifact_type,
                prompt_engine=prompt_engine,
            )
            for skill in skill_registry.list_enabled()
        ]

    async def run(
        self,
        task: str | None = None,
        input: Any | None = None,
        context: dict[str, str] | None = None,
        on_step: Callable[[str, str], Awaitable[None]] | None = None,
        on_token: Callable[[str], Awaitable[None]] | None = None,
        on_content_token: Callable[[str], Awaitable[None]] | None = None,
        on_tool_result: Callable[..., Awaitable[None]] | None = None,
        on_tool_start: Callable[..., Awaitable[None]] | None = None,
        on_tool_progress: Callable[[str, ToolProgress], Awaitable[None]] | None = None,
        on_subagent_event: Callable[..., Awaitable[None]] | None = None,
        context_manager: Any | None = None,
        state: AgentState | None = None,
        audit_logger: Any | None = None,
        artifact_store: Any | None = None,
        model_config: dict[str, str] | None = None,
        session_id: str = "",
        event_bus: Any | None = None,
        use_tree: bool = False,
    ) -> AgentResult:
        """Run the full audit pipeline."""
        if input is not None:
            task = input.task
            input_context: dict[str, str] = {}
            for field_name, field_value in input.model_dump().items():
                if field_value is not None and field_name not in (
                    "task",
                    "explicit_inputs",
                ):
                    if isinstance(field_value, str):
                        input_context[field_name] = field_value
                    else:
                        try:
                            input_context[field_name] = json.dumps(
                                field_value, ensure_ascii=False, default=str
                            )
                        except Exception:
                            input_context[field_name] = str(field_value)[:500]
            if context:
                merged = dict(context)
                merged.update(input_context)
                context = merged
            else:
                context = input_context

        if task is None:
            raise ValueError("Either 'task' or 'input' must be provided")

        self._audit_results = {}

        # Build dispatch context — file_path (when present) is available for
        # the LLM to call parse_document / convert_document.  Sessions without
        # an upload are plain conversations and proceed without it.
        dispatch_context = dict(context) if context else {}

        # Create a root runtime handle so that every SkillTool spawn shares
        # the same budget, depth limit, and cycle detection tree.  Created
        # whenever a runtime exists — skills may arrive mid-run via domain
        # activation, so an empty registry must not leave them without a
        # root anchor.
        from ..runtime.handle import AgentHandle

        root_handle: AgentHandle | None = None
        if self._agent_runtime is not None:
            root_handle = AgentHandle.create(
                agent_name="orchestrator",
                agent_type="orchestrator",
                task=task,
                budget=self._agent_runtime.default_budget,
            )
        # Expose the current run's wiring so DomainActivator can bind
        # SkillTools it injects after this sweep.
        self._subagent_event_callback = on_subagent_event
        self._subagent_root_handle = root_handle

        # Forward sub-agent event streaming and parent handle to SkillTool instances.
        async with self._skill_callback_lock:
            self._attach_skill_callbacks(
                on_subagent_event=on_subagent_event,
                parent_handle=root_handle,
            )

        # LLM-driven audit planning and dispatch (agent_loop)
        result = await super().run(
            task=task,
            input=None,
            context=dispatch_context,
            on_step=on_step,
            on_token=on_token,
            on_content_token=on_content_token,
            on_tool_result=on_tool_result,
            on_tool_start=on_tool_start,
            on_tool_progress=on_tool_progress,
            context_manager=context_manager,
            state=state,
            audit_logger=audit_logger,
            artifact_store=artifact_store,
            model_config=model_config,
            session_id=session_id,
            event_bus=event_bus,
            use_tree=use_tree,
        )

        # Collect audit results from direct skill tool calls.
        skill_names = self._runtime_skill_names()
        for name, data in result.get_named_tool_results().items():
            if name in skill_names and data is not None:
                self._audit_results[name] = data

        return result

    def _attach_skill_callbacks(
        self,
        *,
        on_subagent_event: Callable[..., Awaitable[None]] | None,
        parent_handle: AgentHandle | None = None,
    ) -> None:
        """Wire sub-agent event callbacks and parent handle into every SkillTool.

        Each call creates a fresh shallow copy of the SkillTool and re-registers
        it, so concurrent/reused Orchestrator runs do not overwrite each other's
        callbacks or mutate a shared registry instance.
        """
        for tool in self.tool_registry.list_tools():
            if isinstance(tool, SkillTool):
                copied = copy.copy(tool)
                copied.set_callbacks(on_subagent_event=on_subagent_event)
                copied.set_parent_handle(parent_handle)
                self.tool_registry.register(copied, force=True)

    def _runtime_skill_names(self) -> set[str]:
        """Return the set of skill names registered as runtime tools."""
        return {
            tool.name for tool in self.tool_registry.list_tools() if isinstance(tool, SkillTool)
        }

    @property
    def skill_names(self) -> list[str]:
        """Names of skills registered as runtime tools, in registry order."""
        return [
            tool.name for tool in self.tool_registry.list_tools() if isinstance(tool, SkillTool)
        ]

    @property
    def audit_results(self) -> dict[str, Any]:
        """Aggregated results from all auditors, keyed by auditor name."""
        return copy.deepcopy(self._audit_results)
