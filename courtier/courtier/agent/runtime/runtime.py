"""AgentRuntime — first-class sub-agent lifecycle management."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from courtier.agent.artifacts.scoped_store import ScopedArtifactView
from courtier.agent.core.capability import CapabilityRegistry
from courtier.agent.core.context_manager import ContextManager
from courtier.agent.core.event_bus import EventBus
from courtier.agent.core.memory_manager import MemoryManager
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

    Wraps a SkillConfig.
    """

    name: str
    description: str
    agent_type: Literal["skill", "agent"]
    system_prompt: str
    tools: tuple[str, ...]  # 插件/内置工具名
    skills: tuple[str, ...] = ()  # 子技能名（仅 skill 类型使用）
    input_model: type[Any] | None = None
    output_artifact_type: str | None = None
    source: SkillConfig | None = None
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
    artifact_store: Any | None = None  # ArtifactStore (subsumes the legacy CacheStore)
    summarizer: ResultSummarizer | None = None
    default_budget: AgentRuntimeBudget = field(default_factory=AgentRuntimeBudget)
    session_id: str = ""
    capability_registry: CapabilityRegistry | None = None
    event_bus: EventBus | None = None
    cache_dir: str = ".agent_cache"  # used when a fallback MemoryManager is needed

    def __post_init__(self) -> None:
        self._configs: dict[str, AgentConfig] = {}
        self._handles: dict[str, AgentHandle] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._cumulative_runtime: dict[str, float] = {}
        self._scope_counter: int = 0
        if self.capability_registry is None:
            self.capability_registry = CapabilityRegistry(
                tool_registry=self.tool_registry,
                skill_registry=self.skill_registry,
            )
        if self.event_bus is None:
            self.event_bus = EventBus()
        store = self.artifact_store
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

    def list_agents(self) -> list[str]:
        return sorted(self._configs.keys())

    def spawn(
        self,
        *,
        name: str,
        task: str,
        parent_handle: AgentHandle | None = None,
        context_mode: str = "transparent",
        context: dict[str, str] | None = None,
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

        self._scope_counter += 1
        scope_id = f"scope-{self._scope_counter}"

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
            ref_ids=ref_ids,
            model_config=model_config,
            metadata={"session_id": self.session_id},
            scope_id=scope_id,
        )

        self._handles[handle.handle_id] = handle
        return handle

    def _skill_display_name(self, agent_name: str) -> str | None:
        """Return the Chinese display name for a skill agent, if configured."""
        if self.skill_registry is None:
            return None
        try:
            skill = self.skill_registry.get(agent_name)
        except Exception:
            return None
        if skill is None:
            return None
        return skill.display_name or None

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
                display_name=self._skill_display_name(handle.agent_name),
                detail={
                    "handle_id": handle.handle_id,
                    "parent_handle_id": handle.parent_handle_id,
                    "parent_subagent_name": handle.parent_subagent_name,
                    "scope_id": handle.scope_id,
                },
                parent_subagent_name=handle.parent_subagent_name,
                scope_id=handle.scope_id,
            ),
        )

        scoped_store = self._build_scoped_store(artifact_store, handle)
        if context_manager is not None:
            # Sub-agents share the cache store but get an independent
            # CompactState — their compactions must not interleave with the
            # parent's guard and numbering.
            cm = context_manager.fork()
        else:
            cm = MemoryManager(
                model=self.model,
                cache_dir=self.cache_dir,
                session_id=self.session_id or "default",
            )

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
                    f"({current_cumulative:.1f}s / "
                    f"{handle.budget.max_cumulative_runtime_seconds:.1f}s)"
                ),
                metadata={"handle_id": handle.handle_id},
            )

        start = asyncio.get_running_loop().time()
        task: asyncio.Task | None = None
        try:
            agent = self._build_agent(
                config,
                exclude_skills=set(handle.budget.agent_chain),
                result_store=scoped_store,
            )

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
                    detail={
                        "handle_id": handle.handle_id,
                        "scope_id": handle.scope_id,
                    },
                    parent_subagent_name=handle.parent_subagent_name,
                    scope_id=handle.scope_id,
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

    def _build_agent(
        self,
        config: AgentConfig,
        *,
        exclude_skills: set[str] | None = None,
        result_store: Any = None,
    ) -> "Agent":
        """Construct an Agent instance scoped to the config's declared tools and skills.

        Skills only see the tools listed in their manifest.

        Skill composition (e.g. ``full_government_audit`` dispatching
        ``format_audit``) is opt-in: a skill must explicitly list other skill
        names in its ``skills:`` frontmatter.  Undeclared skills are NOT
        injected — this prevents cross-calling chaos where ``format_audit``
        spawns ``content_audit`` and vice versa.
        """
        from courtier.agent.agents.base import Agent
        from courtier.agent.tools.builtin.skill import SkillTool

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
                # skill_names is only populated when skill_registry exists.
                if name in skill_names and self.skill_registry is not None:
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
            if name in skill_names and self.skill_registry is not None:
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
            agent_name=self._skill_display_name(config.name) or "",
        )
        # Sync summarizer / result_store from the runtime's shared registry
        # so large tool results are summarised before entering the sub-agent's
        # LLM context.  The Agent(tools=…) path does not receive a shared
        # registry, so infrastructure state must be copied explicitly.  When
        # the delegate path provided a scoped artifact view, hand the view
        # (not the shared root store) to the sub-agent's private registry so
        # any result-store access stays inside the sub-agent's scope.
        if hasattr(self.tool_registry, "configure_result_handling"):
            agent.tool_registry.configure_result_handling(
                result_store=(
                    result_store
                    if result_store is not None
                    else getattr(self.tool_registry, "_result_store", None)
                ),
                summarizer=getattr(self.tool_registry, "_summarizer", None),
            )
        return agent

    def _build_context(self, handle: AgentHandle, config: AgentConfig) -> dict[str, str] | None:
        """Build the run-time context for the spawned agent.

        If the parent passed explicit context (e.g. file_path), surface it as
        task context so the sub-agent can call tools like parse_document.
        """
        return handle.context

    def _build_scoped_store(self, artifact_store: Any, handle: AgentHandle) -> Any:
        """Return a live visibility-scoped view over the shared artifact store.

        Sub-agents share the parent's artifact store (so artifacts the parent
        creates both before AND after spawn stay visible), but reads/writes go
        through a ``ScopedArtifactView``:

        - everything the sub-agent writes is stamped with its ``handle_id``,
          hiding it from sibling sub-agents;
        - artifacts written directly to the root store (``subject="unknown"``)
          and artifacts owned by the ancestor chain stay visible;
        - ``handle.ref_ids`` acts as an explicit allow-list, so the
          orchestrator can deliberately hand a sibling's output to a sub-agent.

        When the incoming store is already a view (nested skill dispatch),
        the child view inherits the parent's allowed set plus the parent's own
        scope, so a nested sub-agent sees its whole ancestor chain but never
        siblings or side branches.
        """
        if artifact_store is None:
            return None
        if isinstance(artifact_store, ScopedArtifactView):
            allowed = artifact_store.allowed | {artifact_store.scope}
        else:
            allowed = frozenset()
        return ScopedArtifactView(
            artifact_store,
            scope=handle.handle_id,
            allowed=allowed,
            extra_allowed=handle.ref_ids,
        )

    def _wrap_callbacks(
        self,
        *,
        handle: AgentHandle,
        on_subagent_event: _OnSubagentEvent | None,
        callbacks: dict[str, Callable[..., Awaitable[None]]],
    ) -> dict[str, Any]:
        """Wrap standard agent callbacks to emit sub-agent stream events.

        Original callbacks are preserved and invoked after the event is emitted.
        Events are scoped to *handle.scope_id* and filtered by
        *handle.context_mode*: ``blackbox`` only forwards ``start``/``end``,
        ``transparent`` forwards everything.
        """
        from courtier.agent.agents.subagent.events import SubAgentStreamEvent

        if on_subagent_event is None:
            return callbacks

        def _event(
            kind: str,
            **kwargs: Any,
        ) -> SubAgentStreamEvent:
            return SubAgentStreamEvent(
                kind=kind,  # type: ignore[arg-type]
                subagent_name=handle.agent_name,
                handle_id=handle.handle_id,
                parent_handle_id=handle.parent_handle_id,
                parent_subagent_name=handle.parent_subagent_name,
                scope_id=handle.scope_id,
                **kwargs,
            )

        async def _emit(event: SubAgentStreamEvent) -> None:
            await self._emit_scoped_event(
                handle=handle,
                callback=on_subagent_event,
                event=event,
            )

        async def _on_token(token: str) -> None:
            await _emit(_event("token", text=token, detail={"handle_id": handle.handle_id}))
            original = callbacks.get("on_token")
            if original is not None:
                await original(token)

        async def _on_content_token(token: str) -> None:
            await _emit(_event("conclusion", text=token, detail={"handle_id": handle.handle_id}))
            original = callbacks.get("on_content_token")
            if original is not None:
                await original(token)

        async def _on_step(event: str, detail: str) -> None:
            if event == "think":
                await _emit(_event("think", text=detail, detail={"handle_id": handle.handle_id}))
            original = callbacks.get("on_step")
            if original is not None:
                await original(event, detail)

        async def _on_tool_result(tool_name: str, result: Any, summary: str) -> None:
            success = getattr(result, "success", True)
            metadata = getattr(result, "metadata", None) or {}
            issue_counts = metadata.get("issue_counts")
            await _emit(
                _event(
                    "tool_result",
                    tool_name=tool_name,
                    tool_status="ok" if success else "error",
                    tool_summary=summary,
                    tool_issue_counts=issue_counts if isinstance(issue_counts, dict) else None,
                    detail={"handle_id": handle.handle_id},
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

    async def _emit_event(
        self,
        callback: _OnSubagentEvent | None,
        event: "SubAgentStreamEvent",
    ) -> None:
        """Emit a sub-agent event to the callback and the shared event bus."""
        if self.event_bus is not None:
            from courtier.agent.core.events import AgentEvent

            wrapped = AgentEvent(
                type="subagent.event",
                session_id=self.session_id or "default",
                agent_name=event.subagent_name,
                turn_index=0,
                payload={
                    "subagent_event": event,
                    "handle_id": event.handle_id,
                    "parent_handle_id": event.parent_handle_id,
                    "scope_id": event.scope_id,
                    "context_mode": getattr(event, "context_mode", "transparent"),
                },
            )
            try:
                await self.event_bus.publish(wrapped)
            except Exception:
                logger.exception("Unhandled exception publishing sub-agent event")
        if callback is None:
            return
        try:
            await callback(event)
        except Exception:
            logger.exception("Unhandled exception in on_subagent_event")

    @staticmethod
    async def _emit_scoped_event(
        handle: AgentHandle,
        callback: _OnSubagentEvent | None,
        event: "SubAgentStreamEvent",
    ) -> None:
        """Emit a sub-agent event only if the handle's context mode allows it.

        ``blackbox`` mode suppresses intermediate events (token, think,
        tool_result, conclusion) so the parent only observes start/end.
        ``transparent`` mode forwards everything.
        """
        if handle.context_mode == "blackbox" and event.kind not in {"start", "end"}:
            return
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
        self._cumulative_runtime[root_id] = self._cumulative_runtime.get(root_id, 0.0) + elapsed
