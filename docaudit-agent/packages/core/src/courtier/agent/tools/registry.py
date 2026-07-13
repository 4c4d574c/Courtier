"""ToolRegistry — central registry for tool discovery and execution."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from .protocol import OnToolProgress, ToolProgress, ToolProtocol, ToolResult
from courtier.agent.artifacts.models import (
    ProjectionPolicy, InputField, RuntimePolicy,
    build_contract_from_input_fields, derive_upstream_producers,
)
from courtier.agent.artifacts.projectors import ProjectorRegistry, create_default_projector_registry
from courtier.agent.artifacts.resolver import emit_event
from courtier.agent.artifacts.store import ArtifactStore
from courtier.agent.core.execution_result import ExecutionResult

logger = logging.getLogger(__name__)

class ToolRegistry:
    """Agent 通过它发现和调用工具。

    Thread-safe for reads; register() should be called during setup only.
    """

    def __init__(
        self,
        policy: ProjectionPolicy | None = None,
        projector_registry: ProjectorRegistry | None = None,
        result_store: Any | None = None,
        summarizer: Any | None = None,
    ) -> None:
        self._tools: dict[str, ToolProtocol] = {}
        self._tool_call_counts: dict[str, int] = {}
        self._tool_consecutive_counts: dict[str, int] = {}
        self._last_tool_called: str | None = None
        self._policy = policy or ProjectionPolicy()
        self._projector_registry = projector_registry or create_default_projector_registry()
        self._producer_cache: dict[str, list[str]] | None = None
        self._result_store = result_store
        self._summarizer = summarizer

    @property
    def policy(self) -> ProjectionPolicy:
        return self._policy

    @policy.setter
    def policy(self, value: ProjectionPolicy) -> None:
        self._policy = value

    def reset_run_state(self) -> None:
        """Reset per-run tracking counters (call at start of each agent run)."""
        self._tool_call_counts.clear()
        self._tool_consecutive_counts.clear()
        self._last_tool_called = None

    def register(self, tool: ToolProtocol, force: bool = False) -> None:
        """Register a tool. Raises ValueError on duplicate name unless force=True.

        Contract reachability is NOT validated at registration time because
        upstream producers may not have been registered yet (plugin load
        order is non-deterministic).  Call :meth:`validate_all_contracts`
        after all tools and plugins are loaded.
        """
        if tool.name in self._tools:
            if force:
                del self._tools[tool.name]
            else:
                raise ValueError(f"Duplicate tool name: {tool.name}")
        self._tools[tool.name] = tool
        self._producer_cache = None

    def get(self, name: str) -> ToolProtocol:
        """Look up a tool by name. Raises KeyError if not found."""
        if name not in self._tools:
            raise KeyError(f"Tool not found: {name}")
        return self._tools[name]

    def unregister(self, name: str) -> None:
        """Remove a tool by name. Raises KeyError if not found."""
        if name not in self._tools:
            raise KeyError(f"Tool not found: {name}")
        del self._tools[name]
        self._producer_cache = None

    def list_tools(self) -> list[ToolProtocol]:
        """Return all registered tools."""
        return list(self._tools.values())

    def get_output_schema(self, name: str) -> dict | None:
        """Get the output_schema of a registered tool, or None if not declared."""
        tool = self.get(name)
        return getattr(tool, "output_schema", None)

    def get_schemas(
        self, *, hide_debug_for_task_agents: bool | None = None
    ) -> list[dict[str, Any]]:
        """Return all tool schemas in OpenAI function-calling format.

        When hide_debug_for_task_agents is True, tools with runtime_policy
        hidden_from_task_agents_by_default=True are excluded.
        If None, uses the feature flag from ProjectionPolicy.
        """
        hide_debug = (
            hide_debug_for_task_agents
            if hide_debug_for_task_agents is not None
            else self._policy.features.hide_debug_tools_for_task_agents
        )
        schemas = []
        for tool in self._tools.values():
            runtime_policy = getattr(tool, "runtime_policy", None)
            if hide_debug and runtime_policy is not None:
                if getattr(runtime_policy, "hidden_from_task_agents_by_default", False):
                    continue
            schemas.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
            )
        return schemas

    async def execute(
        self,
        name: str,
        context_manager: Any | None = None,
        cache_store: Any | None = None,
        artifact_store: ArtifactStore | None = None,
        on_tool_start: Callable[[str], Any] | None = None,
        on_tool_progress: Callable[[str, ToolProgress], Any] | None = None,
        audit_logger: Any | None = None,
        **kwargs: Any,
    ) -> ExecutionResult:
        """Execute a tool by name with the given arguments.

        If cache_store is provided:
          - Resolve $ref references in kwargs before execution (type-adaptive).
          - Persist successful results after execution.
        If artifact_store is provided:
          - Auto-bind contract fields from typed artifacts.
          - Register tool output as typed artifact via output_artifact_type.
        """
        tool = self.get(name)

        # --- Enforce runtime policy ---
        runtime_policy = getattr(tool, "runtime_policy", None)
        if runtime_policy is not None:
            refused = self._check_runtime_policy(name, runtime_policy, kwargs)
            if refused is not None:
                return await self._to_execution_result(name, refused)

        if on_tool_start is not None:
            await on_tool_start(name)

        # Auto-bind contract arguments from typed artifacts
        artifact_bindings: dict[str, str] = {}
        input_fields = getattr(tool, "input_fields", None)
        if (
            artifact_store is not None
            and input_fields is not None
            and len(input_fields) > 0
            and self._policy.features.tool_auto_binding_enabled
        ):
            effective_fields = build_contract_from_input_fields(name, input_fields)
            if self._policy.features.resolver_dry_run:
                logger.info(
                    "Dry-run: would auto-bind %s fields from artifacts", name
                )
            else:
                producers = self._get_producers()
                binding_result = self._bind_contract_arguments(
                    fields=effective_fields,
                    tool_name=name,
                    artifact_store=artifact_store,
                    explicit_kwargs=kwargs,
                    producers=producers,
                )
                if not binding_result.success:
                    return await self._to_execution_result(name, binding_result)
                kwargs = {**kwargs, **binding_result.data["arguments"]}
                artifact_bindings = binding_result.data["artifact_bindings"]

        # Resolve refs before execution (type-adaptive).
        # Prefer artifact_store (which now subsumes cache_store functionality).
        skip_resolve = getattr(tool, "skip_ref_resolution", False)
        store = artifact_store or cache_store
        if store is not None and not skip_resolve:
            param_props = tool.parameters.get("properties", {})
            kwargs = store.resolve_refs(kwargs, param_props)
        elif context_manager is not None and not skip_resolve:
            kwargs = context_manager.resolve_refs(kwargs)

        def on_progress(progress: ToolProgress) -> None:
            if on_tool_progress is not None:
                on_tool_progress(name, progress)

        result = await tool.execute(
            on_progress=on_progress,
            context_manager=context_manager,
            artifact_store=artifact_store,
            audit_logger=audit_logger,
            **kwargs,
        )

        # Some tools (e.g. the subagent adapter) already return the unified
        # ExecutionResult.  Legacy tools return ToolResult.
        is_execution_result = isinstance(result, ExecutionResult)

        # Preserve the original data before cache_store replaces it with a $ref marker.
        original_data = result.raw_data if is_execution_result else result.data

        # Persist successful results (unless tool opts out).
        # Use artifact_store for persistence — it now handles both disk I/O
        # (former CacheStore) and typed artifact registration.
        skip = getattr(tool, "skip_persist", False)
        persist_store = artifact_store or cache_store
        if (
            not is_execution_result
            and persist_store is not None
            and result.success
            and result.data is not None
            and not skip
        ):
            if isinstance(result.data, dict) and result.data.get("__persisted_output__"):
                pass
            else:
                source_ref_id = result.metadata.get("source_ref_id")
                source_query = result.metadata.get("source_query")
                label = result.metadata.get("label")
                persist_result = await persist_store.persist(
                    result.data, tool.name,
                    tool_registry=self,
                    source_ref_id=source_ref_id,
                    source_query=source_query,
                    label=label,
                )
                result = result.model_copy(update={"data": persist_result.data})

        metadata_updates: dict[str, Any] = {}
        if artifact_bindings:
            metadata_updates["artifact_bindings"] = artifact_bindings

        if metadata_updates:
            if is_execution_result:
                from dataclasses import replace
                result = replace(result, metadata={**result.metadata, **metadata_updates})
            else:
                result = result.model_copy(
                    update={"metadata": {**result.metadata, **metadata_updates}}
                )

        # Register tool output as typed artifact.
        # ArtifactStore now handles both disk persistence AND typed registration
        # — no separate cache_store bridge needed.  Tool outputs are automatically
        # registered even when the tool doesn't declare output_artifact_type.
        if artifact_store is not None and result.success:
            try:
                skip_artifact_registration = getattr(
                    tool, "skip_artifact_registration", False
                )
                if not skip_artifact_registration:
                    output_artifact_type = getattr(tool, "output_artifact_type", None)
                    if output_artifact_type:
                        self._register_output_artifact_simple(
                            tool_name=name, artifact_type=output_artifact_type,
                            result=result, artifact_store=artifact_store,
                        )
                    else:
                        # Auto-register with derived type — no tool left invisible.
                        derived_type = self._derive_artifact_type(tool_name=name)
                        self._auto_register_artifact(
                            tool_name=name, artifact_type=derived_type,
                            result=result, artifact_store=artifact_store,
                        )
            except Exception:
                logger.warning(
                    "Failed to register output artifact for %s", name, exc_info=True,
                )

        return await self._to_execution_result(
            name, result, original_data=original_data,
            skip_summarize=getattr(tool, "skip_summarize", False),
        )

    def _check_runtime_policy(
        self, name: str, policy: RuntimePolicy, kwargs: dict[str, Any] | None = None
    ) -> ToolResult | None:
        """Enforce runtime policy. Returns ToolResult if blocked, None if allowed."""
        self._tool_call_counts[name] = self._tool_call_counts.get(name, 0) + 1
        if self._last_tool_called == name:
            self._tool_consecutive_counts[name] = (
                self._tool_consecutive_counts.get(name, 0) + 1
            )
        else:
            self._tool_consecutive_counts[name] = 1
        self._last_tool_called = name

        max_calls = policy.max_calls
        max_consecutive = policy.max_consecutive

        if max_calls is not None and self._tool_call_counts[name] > max_calls:
            emit_event("repeated_tool_call_blocked", {
                "tool": name, "reason": "max_calls",
                "count": self._tool_call_counts[name], "limit": max_calls,
            })
            return ToolResult(
                success=False,
                error=(
                    f"Tool '{name}' has been called {self._tool_call_counts[name]} times, "
                    f"exceeding the limit of {max_calls}."
                ),
                metadata={"blocked_reason": "max_calls_exceeded"},
            )
        if max_consecutive is not None and self._tool_consecutive_counts.get(name, 0) > max_consecutive:
            emit_event("repeated_tool_call_blocked", {
                "tool": name, "reason": "max_consecutive_calls",
                "count": self._tool_consecutive_counts[name], "limit": max_consecutive,
            })
            return ToolResult(
                success=False,
                error=(
                    f"Tool '{name}' has been called {self._tool_consecutive_counts[name]} "
                    f"consecutive times, exceeding the limit of {max_consecutive}."
                ),
                metadata={"blocked_reason": "max_consecutive_exceeded"},
            )
        return None

    def _bind_contract_arguments(
        self,
        *,
        fields: tuple[InputField, ...],
        tool_name: str,
        artifact_store: ArtifactStore,
        explicit_kwargs: dict[str, Any],
        producers: dict[str, list[str]] | None = None,
    ) -> ToolResult:
        # Lazy import to break circular dependency:
        # artifacts.binder → artifacts.models → tools.registry
        from courtier.agent.artifacts.binder import ContractBinder
        binder = ContractBinder(artifact_store, self._policy)
        return binder.bind_tool_inputs(
            fields, tool_name, explicit_kwargs, producers=producers,
        )

    def _register_output_artifact_simple(
        self, *, tool_name: str, artifact_type: str,
        result: ToolResult | ExecutionResult, artifact_store: ArtifactStore,
    ) -> None:
        import json
        from pathlib import Path
        if not result.success:
            return
        data = result.raw_data if isinstance(result, ExecutionResult) else result.data
        if data is None:
            return
        # Design note: $ref:<tool>:latest intentionally overwrites
        # the previous run's artifact. The system only tracks the
        # most recent output per tool — if a tool produces a new
        # result, it replaces the old one rather than accumulating.
        ref_id = f"$ref:{tool_name}:latest"
        if isinstance(data, dict) and data.get("__persisted_output__"):
            ref_id = data.get("ref_id", ref_id)
            filepath = data.get("file")
            if filepath:
                try:
                    data = json.loads(Path(filepath).read_text(encoding="utf-8"))
                except (FileNotFoundError, json.JSONDecodeError, OSError):
                    pass
        artifact_store.register_cached_ref(
            ref_id=ref_id, artifact_type=artifact_type,
            created_by=tool_name, data=data,
            role="intermediate", subject="unknown",
            projection_allowed=True, debug_only=False,
        )
        emit_event("artifact_created", {
            "artifact_type": artifact_type,
            "created_by": tool_name, "ref_id": ref_id,
        })

    # -- Artifact type derivation -----------------------------------------------

    # Centralized tool-name → artifact-type mapping.  Formerly scattered across
    # stream_service._TOOL_ARTIFACT_TYPE and individual tool classes.  Tools
    # that declare ``output_artifact_type`` take precedence; this mapping
    # provides sensible defaults for tools that don't.
    _TOOL_ARTIFACT_TYPE: dict[str, str] = {
        "parse_document": "docaudit.parsed_document",
    }

    @classmethod
    def _derive_artifact_type(cls, *, tool_name: str) -> str:
        """Return the artifact type for *tool_name*.

        Falls back to ``"core.cached_output"`` when no mapping exists so that
        every tool output is at least visible in ``list_artifacts``.
        """
        return cls._TOOL_ARTIFACT_TYPE.get(tool_name, "core.cached_output")

    def _auto_register_artifact(
        self, *, tool_name: str, artifact_type: str,
        result: ToolResult | ExecutionResult, artifact_store: ArtifactStore,
    ) -> None:
        """Register a tool output as an artifact without requiring ``output_artifact_type``.

        This is the fallback path for tools that never declared an artifact type.
        It uses the same ``register_cached_ref`` mechanism as the explicit path.
        """
        import json
        from pathlib import Path
        if not result.success:
            return
        data = result.raw_data if isinstance(result, ExecutionResult) else result.data
        if data is None:
            return
        ref_id = f"$ref:{tool_name}:latest"
        if isinstance(data, dict) and data.get("__persisted_output__"):
            ref_id = data.get("ref_id", ref_id)
            filepath = data.get("file")
            if filepath:
                try:
                    data = json.loads(Path(filepath).read_text(encoding="utf-8"))
                except (FileNotFoundError, json.JSONDecodeError, OSError):
                    pass
        # Fallback artifacts are projection-allowed so downstream tools
        # can discover them via list_artifacts.
        is_fallback = artifact_type == "core.cached_output"
        artifact_store.register_cached_ref(
            ref_id=ref_id, artifact_type=artifact_type,
            created_by=tool_name, data=data,
            role="intermediate", subject="unknown",
            projection_allowed=True,
            debug_only=is_fallback,
        )
        emit_event("artifact_created", {
            "artifact_type": artifact_type,
            "created_by": tool_name, "ref_id": ref_id,
        })

    def configure_result_handling(
        self,
        *,
        result_store: Any | None = None,
        summarizer: Any | None = None,
    ) -> None:
        """Attach result-store/summarizer used to wrap ToolResults into ExecutionResults."""
        self._result_store = result_store
        self._summarizer = summarizer

    def _get_producers(self) -> dict[str, list[str]]:
        """返回缓存的 producers 映射，延迟计算."""
        if self._producer_cache is None:
            self._producer_cache = derive_upstream_producers(
                list(self._tools.values()), self._projector_registry,
            )
        return self._producer_cache

    async def _to_execution_result(
        self,
        tool_name: str,
        tool_result: ToolResult | ExecutionResult,
        original_data: Any | None = None,
        *,
        skip_summarize: bool = False,
    ) -> ExecutionResult:
        """Convert a legacy ToolResult (or pass through an existing ExecutionResult).

        *original_data* is the data before cache_store replaced it with a $ref
        marker; it is used for summarization so the parent agent sees content
        rather than a reference handle.

        When *skip_summarize* is ``True`` the summarizer is bypassed entirely
        and the full *data* is placed in ``raw_data``.  Use this for tools
        whose purpose is to retrieve previously-persisted results (e.g.
        ``get_artifact``) so they are not themselves summarised.
        """
        if isinstance(tool_result, ExecutionResult):
            return tool_result

        data = original_data if original_data is not None else tool_result.data
        metadata = {"tool_name": tool_name, **tool_result.metadata}
        if self._summarizer is not None and not skip_summarize:
            return await self._summarizer.from_data(
                success=tool_result.success,
                actor_type="tool",
                actor_name=tool_name,
                data=data,
                error=tool_result.error,
                metadata=metadata,
            )
        if not tool_result.success:
            return ExecutionResult.from_error(
                actor_type="tool",
                actor_name=tool_name,
                error=tool_result.error or "unknown error",
                metadata=metadata,
            )
        return ExecutionResult(
            success=True,
            actor_type="tool",
            actor_name=tool_name,
            raw_data=data,
            metadata=metadata,
        )

    def _validate_contract_reachability(self, tool: ToolProtocol) -> list[str]:
        """检查 input_fields 所需类型是否能被现有系统满足."""
        input_fields = getattr(tool, "input_fields", None)
        if not input_fields:
            return []
        producers = self._get_producers()
        warnings: list[str] = []
        for f in input_fields:
            if f.artifact_type not in producers:
                warnings.append(
                    f"input field '{f.name}' requires artifact type "
                    f"'{f.artifact_type}' which has no known producer"
                )
        return warnings

    def validate_all_contracts(self) -> list[str]:
        """Validate input_field contracts for ALL registered tools.

        Call this once after all plugins are loaded so the full producer
        graph is available.  Returns a list of warning messages for any
        contracts that cannot be satisfied.
        """
        all_warnings: list[str] = []
        for tool in self._tools.values():
            for w in self._validate_contract_reachability(tool):
                all_warnings.append(f"Tool '{tool.name}': {w}")
        for w in all_warnings:
            logger.warning(w)
        return all_warnings
