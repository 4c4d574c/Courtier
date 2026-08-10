"""Agent base class — identity, tools, model, memory."""

from __future__ import annotations

import asyncio
import json
import logging
import platform
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from courtier.agent.core.execution_result import ExecutionResult
from courtier.prompts.engine import PromptEngine

from ..core.audit_logger import AuditLogger
from ..core.context_manager import _is_summary_message
from ..core.event_bus import EventBus
from ..core.loop import agent_loop
from ..core.model import BackendModelClient, ModelClient
from ..core.state import AgentState, AgentStatus, Message
from ..core.tool_call import ToolCall
from ..hooks.chain import HookChain
from ..memory.store import MemoryStore
from ..permissions.gate import PermissionGate
from ..prompts.pipeline import PromptPipeline
from ..tools.protocol import ToolProgress, ToolProtocol
from ..tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


def _has_successful_call_for(messages: tuple[Message, ...], tool_name: str, file_path: str) -> bool:
    """True when *messages* show a successful *tool_name* call for *file_path*.

    Single pass: tool-result payloads are indexed by tool_call_id first, so
    the check is O(n) in history length (the previous double loop with a
    per-pair json.loads was O(n²)).

    Two compacted shapes are recognised in addition to live tool results:
    - micro-compact placeholders (``_omitted``) keep a top-level ``success``
      flag, so ``payload.get("success")`` covers them transparently;
    - full-compaction summaries replaced the whole exchange, so a textual
      fallback looks for the file path together with the tool's $ref (only
      successful calls produce one) or an explicit success marker.
    """
    results_by_id: dict[str, dict[str, Any]] = {}
    summary_blobs: list[str] = []
    for msg in messages:
        if msg.role == "tool" and msg.tool_call_id:
            try:
                payload = json.loads(msg.content or "")
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(payload, dict):
                results_by_id[msg.tool_call_id] = payload
        elif msg.role == "system" and msg.content and _is_summary_message(msg):
            summary_blobs.append(msg.content)

    for msg in messages:
        if msg.role != "assistant" or not msg.tool_calls:
            continue
        for tc in msg.tool_calls:
            if tc.name != tool_name or tc.arguments.get("file_path") != file_path:
                continue
            payload = results_by_id.get(tc.id)
            if payload is not None and payload.get("success"):
                return True

    if summary_blobs:
        ref_marker = f"$ref:{tool_name}:"
        for blob in summary_blobs:
            if file_path not in blob:
                continue
            if ref_marker in blob or (tool_name in blob and "成功" in blob):
                return True
    return False


@dataclass(frozen=True)
class AgentResult:
    """Result of an agent run."""

    status: AgentStatus
    content: str | None = None
    data: dict[str, int] = field(default_factory=dict)
    tool_results: tuple[ExecutionResult, ...] = ()
    termination_reason: str | None = None
    final_state: AgentState | None = None

    @classmethod
    def from_state(cls, state: AgentState, *, include_state: bool = False) -> "AgentResult":
        """Extract result from final agent state."""
        content = None
        for msg in reversed(state.messages):
            if msg.role == "assistant" and msg.content:
                content = msg.content
                break

        return cls(
            status=state.status,
            content=content,
            data={
                "steps": state.current_step,
                "tool_calls_count": len(state.tool_calls),
                "tool_results_count": len(state.tool_results),
            },
            tool_results=state.tool_results,
            termination_reason=state.termination_reason,
            final_state=state if include_state else None,
        )

    def get_named_tool_results(self) -> dict[str, Any]:
        """Extract all tool results keyed by tool name from message history.

        Returns {tool_name: data} for every successful tool result.
        """
        if not self.final_state:
            return {}
        results: dict[str, Any] = {}
        for msg in self.final_state.messages:
            if msg.role != "tool" or not msg.name:
                continue
            if not msg.content:
                continue
            try:
                payload = json.loads(msg.content)
            except (json.JSONDecodeError, TypeError):
                continue
            data = payload.get("raw_data")
            if payload.get("success") and data is not None:
                results[msg.name] = data
        return results


@dataclass(frozen=True)
class RunOptions:
    """Options for Agent.run() — reduces parameter count."""

    task: str | None = None
    input: Any | None = None
    context: dict[str, str] | None = None
    on_step: Callable[[str, str], Awaitable[None]] | None = None
    on_token: Callable[[str], Awaitable[None]] | None = None
    on_content_token: Callable[[str], Awaitable[None]] | None = None
    on_tool_result: Callable[[str, ExecutionResult, str], Awaitable[None]] | None = None
    context_manager: Any | None = None
    state: AgentState | None = None
    audit_logger: AuditLogger | None = None
    artifact_store: Any | None = None


class Agent:
    """每个 Agent 拥有独立的身份、工具集和记忆。

    Usage:
        agent = Agent(
            name="EchoAgent",
            role="You are an echo agent. When asked to echo, use the echo tool.",
            tools=[EchoTool()],
            model=MockModelClient(tool_calls=[...]),
        )
        result = await agent.run("echo hello world")
    """

    def __init__(
        self,
        name: str,
        role: str,
        tools: list[ToolProtocol] | None = None,
        model: ModelClient | None = None,
        backend: Any | None = None,
        memory: MemoryStore | None = None,
        hooks: HookChain | None = None,
        permissions: PermissionGate | None = None,
        tool_registry: ToolRegistry | None = None,
        courtier_md_content: str | None = None,
        prompt_engine: PromptEngine | None = None,
        agent_name: str = "",
        first_required_tool: str | None = None,
    ) -> None:
        self.name = name
        self.role = role
        # Human-facing identity used in the system prompt; falls back to the
        # registry name.  Sub-agents built from skills pass the skill's
        # display_name so the model sees a natural-language identity (e.g.
        # "内容审核") instead of a tool identifier it might try to call.
        self.display_name = agent_name or name
        # When set (and the run carries context["file_path"]), the loop's
        # first tool call is forced to this tool — parse-before-anything.
        self._first_required_tool = first_required_tool
        if model is not None and backend is not None:
            raise ValueError(
                f"Agent '{name}' received both 'model' and 'backend'; provide only one."
            )
        if model is None and backend is None:
            raise ValueError(
                f"Agent '{name}' requires an explicit model or backend; received None. "
                f"Use MockModelClient for tests or provide a real ModelClient/ModelBackend."
            )
        if backend is not None:
            self.model: ModelClient = BackendModelClient(
                backend=backend,
                model=getattr(backend, "_model", "unknown"),
                temperature=getattr(backend, "_temperature", 0.7),
            )
        elif model is not None:
            # The guard above guarantees model is set when backend is None.
            self.model = model
        self.memory = memory
        self.hooks = hooks or HookChain()
        self.permissions = permissions or PermissionGate()

        # Use provided shared registry as base, or create new one.
        # Plugin tools registered after agent construction are discovered
        # via incremental sync on each run() call.
        if tool_registry is not None:
            self._shared_tool_registry: ToolRegistry | None = tool_registry
            self.tool_registry = ToolRegistry()
            for tool in tool_registry.list_tools():
                self.tool_registry.register(tool)
            # Sync infrastructure-level state (summarizer / result_store) from
            # the shared registry so that large tool results are summarised
            # before entering the LLM context.  These are shared references —
            # no per-agent isolation is needed for the persistence layer.
            if hasattr(tool_registry, "configure_result_handling"):
                self.tool_registry.configure_result_handling(
                    result_store=getattr(tool_registry, "_result_store", None),
                    summarizer=getattr(tool_registry, "_summarizer", None),
                )
        else:
            self._shared_tool_registry = None
            self.tool_registry = ToolRegistry()
        for tool in tools or []:
            self.tool_registry.register(tool)

        self._sync_lock = asyncio.Lock()

        # Provider of plugin_name → system_prompt (plugin.yaml tool-usage
        # guidance), supplied by the host (agent_service) via
        # :meth:`set_plugin_prompts_provider`.  Queried on every run() so
        # plugin (re)registrations are reflected without rebuilding the agent.
        self._plugin_prompts_provider: Callable[[], dict[str, str]] | None = None

        # Build system prompt (six-section s10 pattern)
        self._prompt_pipeline = PromptPipeline()

        if prompt_engine is not None:
            thinking_directive = prompt_engine.render("behavioral.thinking_directive")
            behavioral_rules = prompt_engine.render("behavioral.rules")
            tool_invocation_rules = prompt_engine.render("tools.invocation_rules")
            pre_turn_reminder = prompt_engine.render("behavioral.pre_turn_reminder")
            periodic_reminder = prompt_engine.render("behavioral.periodic_reminder")
        else:
            # Fallback for backward compatibility in tests
            from courtier.common.behavioral_rules import (
                BEHAVIORAL_RULES,
                PERIODIC_REMINDER,
                PRE_TURN_REMINDER,
                TOOL_INVOCATION_RULES,
            )

            thinking_directive = ""
            behavioral_rules = BEHAVIORAL_RULES
            tool_invocation_rules = TOOL_INVOCATION_RULES
            pre_turn_reminder = PRE_TURN_REMINDER
            periodic_reminder = PERIODIC_REMINDER

        self._pre_turn_reminder = pre_turn_reminder
        self._periodic_reminder = periodic_reminder

        self._prompt_pipeline.set_identity(
            self.display_name,
            role,
            thinking_directive=thinking_directive,
        )
        self._prompt_pipeline.set_behavioral_rules(behavioral_rules)
        self._tool_invocation_rules = tool_invocation_rules
        all_tools = list(tools or [])
        if all_tools:
            # Compact: list only tool names (full schemas are in the API tools param).
            # Categories help the model navigate without repeating every description.
            tool_names = ", ".join(t.name for t in all_tools)
            self._prompt_pipeline.set_tools(
                f"可用工具: {tool_names}\n\n{self._tool_invocation_rules}"
            )
        # Set dynamic environment context
        self._prompt_pipeline.set_environment(
            {
                "platform": platform.system().lower(),
                "model": getattr(model, "model_name", "") or "",
            }
        )

        # Inject project-level rules (COURTIER.md) if provided
        if courtier_md_content:
            self._prompt_pipeline.set_courtier_md(courtier_md_content)

    @property
    def is_remote(self) -> bool:
        """True for agents that run outside the host process."""
        return False

    def set_plugin_prompts_provider(self, provider: Callable[[], dict[str, str]] | None) -> None:
        """Attach a provider of plugin system_prompts (tool-usage guidance).

        The provider is queried lazily on each run(); passing None disables
        the injection.  This is the host-supplied channel — the core only
        renders whatever the plugins declared.
        """
        self._plugin_prompts_provider = provider
        self._refresh_plugin_guides()

    def _refresh_plugin_guides(self) -> None:
        """Rebuild the tool-usage-notes section from the current provider data.

        Only plugins whose tools this agent actually holds are included, so
        e.g. a chat agent carrying shared plugins never shows domain-plugin
        guidance.
        """
        if self._plugin_prompts_provider is None:
            return
        try:
            prompts = self._plugin_prompts_provider() or {}
        except Exception:
            logger.warning("Plugin system_prompts provider failed", exc_info=True)
            return
        held_plugin_names = {
            plugin_name
            for t in self.tool_registry.list_tools()
            if (plugin_name := getattr(getattr(t, "_client", None), "plugin_name", None))
        }
        sections = [
            f"## {name}\n{text}"
            for name, text in sorted(prompts.items())
            if text and name in held_plugin_names
        ]
        self._prompt_pipeline.set_tool_usage_notes("\n\n".join(sections))

    def build_system_prompt(self, context: dict[str, str] | None = None) -> str:
        """Build the full system prompt."""
        return self._prompt_pipeline.build(context)

    async def run(
        self,
        task: str | None = None,
        input: Any | None = None,
        context: dict[str, str] | None = None,
        on_step: Callable[[str, str], Awaitable[None]] | None = None,
        on_token: Callable[[str], Awaitable[None]] | None = None,
        on_content_token: Callable[[str], Awaitable[None]] | None = None,
        on_tool_result: Callable[[str, ExecutionResult, str], Awaitable[None]] | None = None,
        on_tool_start: Callable[[str], Awaitable[None]] | None = None,
        on_tool_progress: Callable[[str, ToolProgress], Awaitable[None]] | None = None,
        on_subagent_event: Callable[..., Awaitable[None]] | None = None,
        context_manager: Any | None = None,
        state: AgentState | None = None,
        audit_logger: AuditLogger | None = None,
        artifact_store: Any | None = None,
        model_config: dict[str, str] | None = None,
        session_id: str = "",
        event_bus: EventBus | None = None,
        use_tree: bool = False,
    ) -> AgentResult:
        """Entry point: receive task, run agent loop, return result.

        on_step(event, detail): called at each phase transition.
        on_token(token): called with reasoning/chain-of-thought tokens.
        on_content_token(token): called with final content/response tokens.
        on_tool_result(tool_name, result, summary): called for each tool result.
        on_tool_start(tool_name): called when a tool execution begins.
        on_tool_progress(tool_name, progress): called with tool progress updates.
        context_manager: optional ContextManager for three-layer context budget control.
        state: optional existing AgentState for multi-turn continuation.
        event_bus: optional publish/subscribe bus for AgentEvent instances.
        use_tree: when True and no prior state is given, initialise a
            ``ConversationTree`` so the session supports branching/replay.
        """
        # 从 SubAgentInput 提取 task 和 context
        if input is not None:
            task = input.task
            # 将 Input 模型中非 None 的结构化字段注入到 context
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

        # Incremental sync: discover plugin tools registered after construction
        async with self._sync_lock:
            if self._shared_tool_registry is not None:
                from ..tools.scoped import ScopedTool

                existing = {t.name: t for t in self.tool_registry.list_tools()}
                for tool in self._shared_tool_registry.list_tools():
                    current = existing.get(tool.name)
                    if current is None:
                        self.tool_registry.register(tool)
                        continue
                    current_inner = current._inner if isinstance(current, ScopedTool) else current
                    if current_inner is tool:
                        continue  # already up to date
                    if getattr(tool, "_client", None) is None:
                        continue  # only plugin proxies need hot-replacement
                    # Plugin restarted: the shared registry now holds a new
                    # proxy (new JSON-RPC client) for the same tool name.
                    # Replace the stale copy so calls don't keep failing
                    # against the dead client — preserving ScopedTool
                    # wrappers (owner scoping) around the new proxy.
                    if isinstance(current, ScopedTool):
                        tool = ScopedTool(tool, current._injections)
                    self.tool_registry.register(tool, force=True)

            # ContextManager.get_ref_instructions() mentions list_artifacts and
            # get_artifact in every agent's system prompt, so ensure they are
            # actually callable when a context_manager is provided.
            if context_manager is not None:
                self._ensure_builtin_artifact_tools()

        # Refresh tool names in the system prompt after syncing plugins
        all_tools = self.tool_registry.list_tools()
        if all_tools:
            tool_names = ", ".join(t.name for t in all_tools)
            self._prompt_pipeline.set_tools(
                f"可用工具: {tool_names}\n\n{self._tool_invocation_rules}"
            )
        # Refresh plugin tool-usage notes (plugin system_prompts) as well —
        # plugins may have registered/restarted since the last run.
        self._refresh_plugin_guides()

        # Inject context-manager ref instructions into the pipeline
        if context_manager is not None:
            self._prompt_pipeline.set_context_instructions(context_manager.get_ref_instructions())

        system_prompt = self.build_system_prompt(context)

        if state is not None:
            messages = list(state.messages)
            if messages and messages[0].role == "system":
                messages[0] = Message(role="system", content=system_prompt)
            # Sub-agent runs arrive with AgentState.initial(task=...) whose
            # last message already is the task — appending it again would
            # duplicate the task in the sub-agent's context.  Multi-turn
            # continuation (history state + new task) always appends.
            if not (messages and messages[-1].role == "user" and messages[-1].content == task):
                messages.append(Message(role="user", content=task))
            current_state = state.model_copy(
                update={
                    "status": "idle",
                    "messages": tuple(messages),
                    "tool_calls": (),
                    "tool_results": (),
                    "current_step": state.current_step,
                    "max_steps": state.max_steps,
                    "termination_reason": None,
                },
            )
        else:
            current_state = AgentState.initial(
                task=task,
                system_prompt=system_prompt,
                use_tree=use_tree,
            )

        from courtier.agent.telemetry.metrics import (
            record_agent_latency,
            record_agent_request,
        )

        forced_first_tool_call = self._maybe_forced_first_tool(context, state)

        start = time.time()
        try:
            final_state = await agent_loop(
                state=current_state,
                model=self.model,
                tool_registry=self.tool_registry,
                hooks=self.hooks,
                permissions=self.permissions,
                on_step=on_step,
                on_token=on_token,
                on_content_token=on_content_token,
                on_tool_result=on_tool_result,
                on_tool_start=on_tool_start,
                on_tool_progress=on_tool_progress,
                context_manager=context_manager,
                audit_logger=audit_logger,
                artifact_store=artifact_store,
                session_id=session_id,
                agent_name=self.name,
                event_bus=event_bus,
                forced_first_tool_call=forced_first_tool_call,
                pre_turn_reminder=self._pre_turn_reminder,
                periodic_reminder=self._periodic_reminder,
            )
        except Exception:
            record_agent_request(agent_name=self.name, status="error")
            raise
        elapsed = time.time() - start
        # record_agent_request is emitted once inside agent_loop for every
        # terminal path; the except branch above covers failures that escape
        # the loop. Only latency is recorded here to avoid double counting.
        record_agent_latency(agent_name=self.name, seconds=elapsed)
        return AgentResult.from_state(final_state, include_state=True)

    def _maybe_forced_first_tool(
        self,
        context: dict[str, str] | None,
        state: AgentState | None,
    ) -> ToolCall | None:
        """Parse-before-anything policy for runs carrying an uploaded document.

        When ``context['file_path']`` is present and the prior history has no
        successful call for the same path, the loop's first tool call is
        forced to the required parsing tool.  Continuations with an unchanged
        file skip the re-parse; a new upload (different path) triggers it.
        """
        tool_name = self._first_required_tool
        if not tool_name:
            return None
        file_path = (context or {}).get("file_path")
        if not file_path:
            return None
        if tool_name not in {t.name for t in self.tool_registry.list_tools()}:
            return None
        if state is not None and _has_successful_call_for(state.messages, tool_name, file_path):
            return None
        # Unique suffix: the same id must never appear twice in one history
        # (an id reused across turns breaks tool-call pairing on resume).
        return ToolCall(
            id=f"forced-first-{tool_name}-{uuid4().hex[:8]}",
            name=tool_name,
            arguments={"file_path": file_path},
        )

    def _ensure_builtin_artifact_tools(self) -> None:
        """Register list_artifacts/get_artifact if the agent lacks them.

        ContextManager.get_ref_instructions() advertises these tools in the
        system prompt of every agent that receives a context_manager, so they
        must be present in the registry to avoid "Tool not found" runtime
        errors when the LLM tries to use them.
        """
        existing = {t.name for t in self.tool_registry.list_tools()}
        if "list_artifacts" in existing and "get_artifact" in existing:
            return

        # Lazy import to avoid circular dependencies at module load time.
        from ..tools.builtin.get_artifact import GetArtifactTool
        from ..tools.builtin.list_artifacts import ListArtifactsTool

        if "list_artifacts" not in existing:
            self.tool_registry.register(ListArtifactsTool())
        if "get_artifact" not in existing:
            self.tool_registry.register(GetArtifactTool())


def parse_audit_result(
    result: AgentResult,
    fallback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Extract structured audit result from AgentResult content."""
    content = result.content or ""
    try:
        parsed = json.loads(content)
        if isinstance(parsed, dict):
            return parsed
    except (json.JSONDecodeError, TypeError):
        pass
    parsed = dict(fallback) if fallback is not None else {"summary": content}
    parsed.setdefault("parse_error", True)
    return parsed
