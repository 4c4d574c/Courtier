"""AgentRuntime — first-class sub-agent lifecycle management."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from courtier.agent.artifacts.store import ArtifactStore
from courtier.agent.core.context_manager import ContextManager
from courtier.agent.core.model import ModelClient
from courtier.agent.core.state import AgentState
from courtier.agent.skills.config import SkillConfig
from courtier.agent.skills.registry import SkillRegistry
from courtier.agent.tools.registry import ToolRegistry

from .budget import AgentRuntimeBudget
from .handle import AgentHandle
from .result import ExecutionResult
from .summarizer import ResultSummarizer

if TYPE_CHECKING:
    from courtier.agent.agents.base import Agent, AgentResult
    from courtier.agent.agents.subagent.config import SubAgentConfig
    from courtier.agent.agents.subagent.events import SubAgentStreamEvent

logger = logging.getLogger(__name__)

_OnSubagentEvent = Callable[..., Awaitable[None]]

# Minimum character length for an agent's final text response to be considered
# a meaningful synthesis. Shorter text is treated as a wrapper/planning utterance
# and we fall back to the last successful tool result instead.
_MIN_SYNTHESIS_LENGTH = 50


@dataclass
class AgentConfig:
    """Runtime-facing agent configuration.

    Wraps either a SkillConfig or a legacy SubAgentConfig.
    """

    name: str
    description: str
    agent_type: Literal["skill", "agent"]
    system_prompt: str
    tools: tuple[str, ...]  # 插件/内置工具名
    skills: tuple[str, ...] = ()  # 子技能名（仅 skill 类型使用）
    input_model: type[Any] | None = None
    output_artifact_type: str | None = None
    source: SkillConfig | SubAgentConfig | None = None
    failure_strategy: Any | None = None
    max_retries: int = 0
    timeout_seconds: float = 0
    max_runtime_seconds: float | None = None
    max_turns: int | None = None


@dataclass
class AgentRuntime:
    """Manages spawned sub-agents, budgets, and result summarization."""

    tool_registry: ToolRegistry
    model: ModelClient
    skill_registry: SkillRegistry | None = None
    cache_store: Any | None = None  # Deprecated — use artifact_store
    artifact_store: Any | None = None  # ArtifactStore (now subsumes cache_store)
    summarizer: ResultSummarizer | None = None
    default_budget: AgentRuntimeBudget = field(default_factory=AgentRuntimeBudget)
    session_id: str = ""

    def __post_init__(self) -> None:
        self._configs: dict[str, AgentConfig] = {}
        self._handles: dict[str, AgentHandle] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._cumulative_runtime: dict[str, float] = {}
        store = self.artifact_store or self.cache_store
        if self.summarizer is None and store is not None:
            self.summarizer = ResultSummarizer(artifact_store=store)
        if hasattr(self.tool_registry, "configure_result_handling"):
            self.tool_registry.configure_result_handling(
                result_store=None,
                summarizer=self.summarizer,
            )
        self._register_skills()

    def _register_skills(self) -> None:
        if self.skill_registry is None:
            return
        available_tools = {t.name for t in self.tool_registry.list_tools()}
        available_skills: set[str] = set()
        if self.skill_registry is not None:
            available_skills = {s.name for s in self.skill_registry.list_enabled()}
        for skill in self.skill_registry.list_enabled():
            # Validate tools against ToolRegistry
            missing_tools = [name for name in skill.tools if name not in available_tools]
            if missing_tools:
                logger.debug(
                    "Skill %r references tools not yet registered (plugins may not be loaded): %s",
                    skill.name,
                    ", ".join(missing_tools),
                )
            # Validate skills against SkillRegistry
            missing_skills = [name for name in skill.skills if name not in available_skills]
            if missing_skills:
                logger.warning(
                    "Skill %r references unknown skills: %s",
                    skill.name,
                    ", ".join(missing_skills),
                )
            self._configs[skill.name] = AgentConfig(
                name=skill.name,
                description=skill.description,
                agent_type="skill",
                system_prompt=skill.system_prompt,
                tools=skill.tools,
                skills=skill.skills,
                input_model=skill.input_model,
                output_artifact_type=skill.output_artifact_type,
                source=skill,
            )

    def register_agent(self, name: str, config: "SubAgentConfig") -> None:
        """Register a legacy SubAgentConfig as a runtime-managed agent."""

        self._configs[name] = AgentConfig(
            name=name,
            description=config.description or name,
            agent_type="agent",
            system_prompt=config.agent.role,
            tools=tuple(t.name for t in config.agent.tool_registry.list_tools()),
            input_model=config.input_model,
            output_artifact_type=config.output_artifact_type,
            source=config,
            failure_strategy=config.failure_strategy,
            max_retries=config.max_retries,
            timeout_seconds=config.timeout_seconds,
            max_runtime_seconds=(
                config.timeout_seconds if config.timeout_seconds > 0 else None
            ),
        )

    def list_agents(self) -> list[str]:
        return sorted(self._configs.keys())

    def spawn(
        self,
        *,
        name: str,
        task: str,
        parent_handle: AgentHandle | None = None,
        context_mode: str = "blackbox",
        context: dict[str, str] | None = None,
        artifact_store: Any = None,
        ref_ids: list[str] | None = None,
        model_config: dict[str, Any] | None = None,
        budget: AgentRuntimeBudget | None = None,
    ) -> AgentHandle:
        """Spawn a sub-agent and return its handle."""
        config = self._configs.get(name)
        if config is None:
            raise ValueError(f"Unknown agent or skill: {name!r}")

        if parent_handle is not None:
            effective_budget = parent_handle.budget
        elif budget is not None:
            effective_budget = budget
        else:
            effective_budget = self.default_budget

        allowed, reason = effective_budget.can_spawn(name)
        if not allowed:
            raise RuntimeError(f"Cannot spawn {name!r}: {reason}")

        handle_id = f"h-{uuid4().hex[:16]}"
        child_budget = effective_budget.allocate_child(
            agent_name=name,
            handle_id=handle_id,
            max_runtime_seconds=config.max_runtime_seconds,
            max_turns=config.max_turns,
        )

        artifact_context: list[dict[str, Any]] = []
        if artifact_store is not None:
            artifact_context = self._build_artifact_context(artifact_store)

        handle = AgentHandle.create(
            handle_id=handle_id,
            agent_name=name,
            agent_type=config.agent_type,
            task=task,
            budget=child_budget,
            parent_handle_id=parent_handle.handle_id if parent_handle else None,
            parent_subagent_name=parent_handle.agent_name if parent_handle else None,
            context_mode=context_mode,
            context=context,
            artifact_context=artifact_context,
            ref_ids=ref_ids,
            model_config=model_config,
            metadata={"session_id": self.session_id},
        )

        self._handles[handle.handle_id] = handle
        return handle

    async def delegate(
        self,
        handle: AgentHandle,
        *,
        artifact_store: Any = None,
        context_manager: ContextManager | None = None,
        audit_logger: Any = None,
        on_subagent_event: _OnSubagentEvent | None = None,
        callbacks: dict[str, Callable[..., Awaitable[None]]] | None = None,
    ) -> ExecutionResult:
        """Run the sub-agent referenced by *handle* and return an ExecutionResult."""
        from courtier.agent.agents.subagent.events import SubAgentStreamEvent

        config = self._configs.get(handle.agent_name)
        if config is None:
            return ExecutionResult.from_error(
                actor_type=config.agent_type if config else "agent",
                actor_name=handle.agent_name,
                error=f"Unknown agent or skill: {handle.agent_name!r}",
            )

        await self._emit_event(
            on_subagent_event,
            SubAgentStreamEvent(
                kind="start",
                subagent_name=handle.agent_name,
                handle_id=handle.handle_id,
                parent_handle_id=handle.parent_handle_id,
                task=handle.task,
                detail={
                    "handle_id": handle.handle_id,
                    "parent_handle_id": handle.parent_handle_id,
                    "parent_subagent_name": handle.parent_subagent_name,
                },
                parent_subagent_name=handle.parent_subagent_name,
            ),
        )

        scoped_store = self._build_scoped_store(artifact_store)
        cm = context_manager or ContextManager()

        wrapped_callbacks = self._wrap_callbacks(
            handle=handle,
            on_subagent_event=on_subagent_event,
            callbacks=callbacks or {},
        )

        root_id = self._root_handle_id(handle)
        runtime_seconds = handle.budget.max_runtime_seconds

        current_cumulative = self._cumulative_runtime.get(root_id, 0.0)
        if current_cumulative >= handle.budget.max_cumulative_runtime_seconds:
            return ExecutionResult.from_error(
                actor_type=config.agent_type,
                actor_name=handle.agent_name,
                error=(
                    f"Cumulative runtime budget exhausted "
                    f"({current_cumulative:.1f}s / {handle.budget.max_cumulative_runtime_seconds:.1f}s)"
                ),
                metadata={"handle_id": handle.handle_id},
            )

        start = asyncio.get_running_loop().time()
        task: asyncio.Task | None = None
        try:
            agent = self._build_agent(config, exclude_skills=set(handle.budget.agent_chain))

            # Wire parent handle and event callbacks into any SkillTools
            # the sub-agent carries, so nested skill calls inherit the
            # correct budget chain and can emit streaming events.
            from courtier.agent.tools.builtin.skill import SkillTool as _SkillTool

            for _tool in agent.tool_registry.list_tools():
                if isinstance(_tool, _SkillTool):
                    _tool.set_parent_handle(handle)
                    if on_subagent_event is not None:
                        _tool.set_callbacks(on_subagent_event=on_subagent_event)

            # Create a dedicated AuditLogger for the sub-agent so its turn
            # records land in a subdirectory separate from the orchestrator's.
            sub_audit_logger: Any = None
            if audit_logger is not None:
                from courtier.agent.core.audit_logger import AuditLogger

                # Derive the sub-agent run_id from the parent's run_id
                # so the directory structure is:
                #   .agent_logs/<session_id>/subagents/<agent_name>/turn_000/
                parent_run_id = getattr(audit_logger, "_run_id", None)
                parent_log_dir = getattr(audit_logger, "_log_dir", None)
                if parent_run_id is not None and parent_log_dir is not None:
                    sub_audit_logger = AuditLogger(
                        log_dir=parent_log_dir,
                        agent_name=handle.agent_name,
                        run_id=f"{parent_run_id}/subagents/{handle.agent_name}",
                    )

            run_kwargs: dict[str, Any] = {
                "task": handle.task,
                "context": self._build_context(handle, config),
                "context_manager": cm,
                "artifact_store": scoped_store,
                "artifact_context": handle.artifact_context,
                "audit_logger": sub_audit_logger or audit_logger,
                "session_id": self.session_id,
                **wrapped_callbacks,
            }
            if handle.model_config:
                run_kwargs["model_config"] = handle.model_config

            initial_state = AgentState.initial(
                task=handle.task,
                system_prompt=agent.build_system_prompt(run_kwargs.get("context")),
                max_steps=handle.budget.max_turns,
            )
            run_kwargs["state"] = initial_state

            task = asyncio.create_task(agent.run(**run_kwargs))
            self._tasks[handle.handle_id] = task
            if runtime_seconds > 0:
                result = await asyncio.wait_for(task, timeout=runtime_seconds)
            else:
                result = await task

            return await self._result_from_agent_result(handle, config, result)

        except asyncio.TimeoutError:
            return ExecutionResult.from_error(
                actor_type=config.agent_type,
                actor_name=handle.agent_name,
                error=f"Sub-agent timed out after {runtime_seconds}s",
                metadata={
                    "handle_id": handle.handle_id,
                    "timeout_seconds": runtime_seconds,
                },
            )
        except asyncio.CancelledError:
            return ExecutionResult.from_error(
                actor_type=config.agent_type,
                actor_name=handle.agent_name,
                error="Sub-agent was terminated",
                metadata={"handle_id": handle.handle_id},
            )
        except Exception as exc:
            logger.exception("AgentRuntime delegate failed for %s", handle.agent_name)
            return ExecutionResult.from_error(
                actor_type=config.agent_type,
                actor_name=handle.agent_name,
                error=str(exc),
                metadata={"handle_id": handle.handle_id},
            )
        finally:
            if task is not None:
                self._tasks.pop(handle.handle_id, None)
            elapsed = asyncio.get_running_loop().time() - start
            self._record_runtime(root_id, elapsed)
            await self._emit_event(
                on_subagent_event,
                SubAgentStreamEvent(
                    kind="end",
                    subagent_name=handle.agent_name,
                    handle_id=handle.handle_id,
                    parent_handle_id=handle.parent_handle_id,
                    detail={"handle_id": handle.handle_id},
                    parent_subagent_name=handle.parent_subagent_name,
                ),
            )

    async def terminate(self, handle: AgentHandle) -> None:
        """Cancel a running sub-agent if possible."""
        task = self._tasks.pop(handle.handle_id, None)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    def _build_agent(self, config: AgentConfig, *, exclude_skills: set[str] | None = None) -> "Agent":
        """Construct an Agent instance scoped to the config's declared tools and skills.

        Skills only see the tools listed in their manifest; legacy SubAgentConfig
        reuses the tools from its configured agent.

        Skill composition (e.g. ``full_government_audit`` dispatching
        ``format_audit``) is opt-in: a skill must explicitly list other skill
        names in its ``skills:`` frontmatter.  Undeclared skills are NOT
        injected — this prevents cross-calling chaos where ``format_audit``
        spawns ``content_audit`` and vice versa.
        """
        from courtier.agent.agents.base import Agent
        from courtier.agent.agents.subagent.config import SubAgentConfig
        from courtier.agent.tools.builtin.skill import SkillTool

        if isinstance(config.source, SubAgentConfig):
            tools = list(config.source.agent.tool_registry.list_tools())
        else:
            resolved: list[Any] = []
            skill_names: set[str] = set()
            if self.skill_registry is not None:
                skill_names = {s.name for s in self.skill_registry.list_enabled()}

            # Resolve tools: look up in ToolRegistry first, with skill fallback
            # for backward compatibility (old format mixed tools + skills in one field).
            for name in config.tools:
                try:
                    tool = self.tool_registry.get(name)
                    resolved.append(tool)
                except KeyError:
                    # Backward compat: tool name matching another skill → inject as SkillTool.
                    if name in skill_names:
                        skill = self.skill_registry.get(name)
                        if skill is not None:
                            resolved.append(
                                SkillTool(
                                    skill=skill,
                                    runtime=self,
                                    output_artifact_type=skill.output_artifact_type,
                                )
                            )
                    else:
                        logger.warning(
                            "Skill %r references unavailable tool: %s",
                            config.name,
                            name,
                        )

            # Resolve skills: look up directly in SkillRegistry.
            for name in config.skills:
                if name in skill_names:
                    skill = self.skill_registry.get(name)
                    if skill is not None:
                        resolved.append(
                            SkillTool(
                                skill=skill,
                                runtime=self,
                                output_artifact_type=skill.output_artifact_type,
                            )
                        )
                else:
                    logger.warning(
                        "Skill %r references unknown skill: %s",
                        config.name,
                        name,
                    )

            tools = resolved

        agent = Agent(
            name=config.name,
            role=config.system_prompt,
            model=self.model,
            tools=tools,
        )
        # Sync summarizer / result_store from the runtime's shared registry
        # so large tool results are summarised before entering the sub-agent's
        # LLM context.  The Agent(tools=…) path does not receive a shared
        # registry, so infrastructure state must be copied explicitly.
        if hasattr(self.tool_registry, "configure_result_handling"):
            agent.tool_registry.configure_result_handling(
                result_store=getattr(self.tool_registry, "_result_store", None),
                summarizer=getattr(self.tool_registry, "_summarizer", None),
            )
        return agent

    def _build_context(
        self, handle: AgentHandle, config: AgentConfig
    ) -> dict[str, str] | None:
        """Build the run-time context for the spawned agent.

        If the parent passed explicit context (e.g. file_path), surface it as
        task context so the sub-agent can call tools like parse_document.
        """
        return handle.context

    def _build_scoped_store(self, artifact_store: Any) -> Any:
        # Sub-agents share the parent's artifact store directly so they can
        # see artifacts created by the parent both before AND after spawn.
        # The snapshot-based ScopedArtifactStore was causing sub-agents to
        # see empty artifact lists because it only captured artifacts that
        # existed at spawn time.  Uniform access is the intended model —
        # sub-agents are trusted components, not sandboxed adversaries.
        return artifact_store

    def _build_artifact_context(self, artifact_store: Any) -> list[dict[str, Any]]:
        if not isinstance(artifact_store, ArtifactStore):
            return []
        entries: list[dict[str, Any]] = []
        for artifact in artifact_store.list_all():
            if artifact.metadata.debug_only:
                continue
            entries.append(
                {
                    "artifact_id": artifact.artifact_id,
                    "artifact_type": artifact.artifact_type,
                    "created_by": artifact.metadata.created_by,
                    "content_hash": artifact.metadata.content_hash,
                    "semantic_role": artifact.metadata.semantic_role,
                }
            )
        return entries

    def _wrap_callbacks(
        self,
        *,
        handle: AgentHandle,
        on_subagent_event: _OnSubagentEvent | None,
        callbacks: dict[str, Callable[..., Awaitable[None]]],
    ) -> dict[str, Any]:
        """Wrap standard agent callbacks to emit sub-agent stream events.

        Original callbacks are preserved and invoked after the event is emitted.
        """
        from courtier.agent.agents.subagent.events import SubAgentStreamEvent

        if on_subagent_event is None:
            return callbacks

        async def _on_token(token: str) -> None:
            await on_subagent_event(
                SubAgentStreamEvent(
                    kind="token",
                    subagent_name=handle.agent_name,
                    handle_id=handle.handle_id,
                    parent_handle_id=handle.parent_handle_id,
                    text=token,
                    detail={"handle_id": handle.handle_id},
                    parent_subagent_name=handle.parent_subagent_name,
                )
            )
            original = callbacks.get("on_token")
            if original is not None:
                await original(token)

        async def _on_content_token(token: str) -> None:
            await on_subagent_event(
                SubAgentStreamEvent(
                    kind="conclusion",
                    subagent_name=handle.agent_name,
                    handle_id=handle.handle_id,
                    parent_handle_id=handle.parent_handle_id,
                    text=token,
                    detail={"handle_id": handle.handle_id},
                    parent_subagent_name=handle.parent_subagent_name,
                )
            )
            original = callbacks.get("on_content_token")
            if original is not None:
                await original(token)

        async def _on_step(event: str, detail: str) -> None:
            if event == "think":
                await on_subagent_event(
                    SubAgentStreamEvent(
                        kind="think",
                        subagent_name=handle.agent_name,
                        handle_id=handle.handle_id,
                        parent_handle_id=handle.parent_handle_id,
                        text=detail,
                        detail={"handle_id": handle.handle_id},
                        parent_subagent_name=handle.parent_subagent_name,
                    )
                )
            original = callbacks.get("on_step")
            if original is not None:
                await original(event, detail)

        async def _on_tool_result(tool_name: str, result: Any, summary: str) -> None:
            success = getattr(result, "success", True)
            await on_subagent_event(
                SubAgentStreamEvent(
                    kind="tool_result",
                    subagent_name=handle.agent_name,
                    handle_id=handle.handle_id,
                    parent_handle_id=handle.parent_handle_id,
                    tool_name=tool_name,
                    tool_status="ok" if success else "error",
                    tool_summary=summary,
                    detail={"handle_id": handle.handle_id},
                    parent_subagent_name=handle.parent_subagent_name,
                )
            )
            original = callbacks.get("on_tool_result")
            if original is not None:
                await original(tool_name, result, summary)

        return {
            **callbacks,
            "on_token": _on_token,
            "on_content_token": _on_content_token,
            "on_step": _on_step,
            "on_tool_result": _on_tool_result,
        }

    async def _result_from_agent_result(
        self,
        handle: AgentHandle,
        config: AgentConfig,
        result: "AgentResult",
    ) -> ExecutionResult:

        success = result.status == "completed"
        data = self._extract_agent_data(result)

        if self.summarizer is not None:
            return await self.summarizer.from_data(
                success=success,
                actor_type=config.agent_type,
                actor_name=config.name,
                data=data,
                error=result.termination_reason if not success else None,
                metadata={
                    "handle_id": handle.handle_id,
                    "session_id": self.session_id,
                    "agent_type": config.agent_type,
                },
            )

        # Fallback when no summarizer is configured.
        return ExecutionResult(
            success=success,
            actor_type=config.agent_type,
            actor_name=config.name,
            raw_data=data if success else None,
            error=result.termination_reason if not success else None,
            metadata={"handle_id": handle.handle_id},
        )

    @staticmethod
    def _extract_agent_data(result: AgentResult) -> Any:
        # Prefer the agent's own final text response when it produced a
        # meaningful synthesis (e.g. orchestration skills like
        # full_government_audit that aggregate results from nested skills).
        # Fall back to the last successful tool result otherwise (leaf skills
        # whose output is primarily the tool data, not agent text).
        content = (result.content or "").strip()
        if len(content) >= _MIN_SYNTHESIS_LENGTH:
            return content
        if result.tool_results:
            for tr in reversed(result.tool_results):
                if tr.success and tr.raw_data is not None:
                    return tr.raw_data
        return result.content

    @staticmethod
    async def _emit_event(
        callback: _OnSubagentEvent | None,
        event: "SubAgentStreamEvent",
    ) -> None:

        if callback is None:
            return
        try:
            await callback(event)
        except Exception:
            logger.exception("Unhandled exception in on_subagent_event")

    def _root_handle_id(self, handle: AgentHandle) -> str:
        """Return the root handle id for cumulative runtime tracking."""
        chain = handle.budget.parent_chain
        return chain[0] if chain else handle.handle_id

    def _record_runtime(self, root_id: str, elapsed: float) -> None:
        self._cumulative_runtime[root_id] = (
            self._cumulative_runtime.get(root_id, 0.0) + elapsed
        )
