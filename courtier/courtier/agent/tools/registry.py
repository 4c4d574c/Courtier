"""ToolRegistry — central registry for tool discovery and execution."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, cast

from courtier.agent.artifacts.models import (
    InputField,
    ProjectionPolicy,
    RuntimePolicy,
    build_contract_from_input_fields,
    derive_upstream_producers,
    stable_content_hash,
)
from courtier.agent.artifacts.projectors import ProjectorRegistry, create_default_projector_registry
from courtier.agent.artifacts.resolver import emit_event
from courtier.agent.artifacts.store import ArtifactStore
from courtier.agent.core.execution_result import ExecutionResult
from courtier.prompts.errors import render_error

from .param_injection import HOST_INJECTED_MARKER
from .protocol import (
    ToolInfo,
    ToolProgress,
    ToolProtocol,
    ToolResult,
    ToolVersioned,
)

logger = logging.getLogger(__name__)


def _log_progress_task_exception(task: "asyncio.Task[None]") -> None:
    """Done-callback for fire-and-forget progress coroutines: surface the
    exception instead of silently discarding it."""
    if not task.cancelled() and task.exception() is not None:
        logger.warning(
            "tool on_progress callback failed",
            exc_info=task.exception(),
        )

#: Result post-processor signature: async (tool_name, kwargs, tool_result)
#: -> tool_result | None.  Runs after tool.execute and BEFORE persistence
#: so the stored artifact, the summarizer view, and the model observation
#: all see the same final payload.
ResultPostProcessor = Callable[[str, dict[str, Any], Any], Any]


def _strip_host_injected_params(parameters: dict[str, Any]) -> dict[str, Any]:
    """Drop host-injected parameters from the model-visible tool schema.

    These are filled at the dispatch boundary (param_injection), so the
    model must neither see nor fill them.  Returns the original mapping
    when nothing is hidden; never mutates the tool's own schema.
    """
    props = parameters.get("properties") or {}
    hidden = {
        key
        for key, prop in props.items()
        if isinstance(prop, dict) and prop.get(HOST_INJECTED_MARKER)
    }
    if not hidden:
        return parameters
    filtered = {key: value for key, value in props.items() if key not in hidden}
    exposed: dict[str, Any] = {**parameters, "properties": filtered}
    if "required" in parameters:
        exposed["required"] = [r for r in (parameters.get("required") or []) if r not in hidden]
    return exposed


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
        self._versions: dict[str, dict[str, ToolProtocol]] = {}
        self._tool_info: dict[str, dict[str, ToolInfo]] = {}
        self._tool_call_counts: dict[str, int] = {}
        self._tool_consecutive_counts: dict[str, int] = {}
        self._last_tool_called: str | None = None
        self._policy = policy or ProjectionPolicy()
        self._projector_registry = projector_registry or create_default_projector_registry()
        self._producer_cache: dict[str, list[str]] | None = None
        self._result_store = result_store
        self._summarizer = summarizer
        self._param_injectors: dict[str, Any] = {}
        self._result_post_processors: list[ResultPostProcessor] = []
        self._policy_lock = asyncio.Lock()

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

    def clone(self) -> "ToolRegistry":
        """Per-session shallow copy: same tool instances, fresh bookkeeping.

        Shares policy/projector/result_store/summarizer by reference;
        registrations and per-run counters are independent, so wrapping a
        tool (e.g. ScopedTool owner-scope injection), re-registering SkillTool
        callbacks, or resetting run state on a clone never leaks into the
        base registry or other concurrent sessions.
        """
        clone = ToolRegistry(
            policy=self._policy,
            projector_registry=self._projector_registry,
            result_store=self._result_store,
            summarizer=self._summarizer,
        )
        for tool in self._tools.values():
            clone.register(tool, force=True)
        clone._param_injectors = dict(self._param_injectors)
        clone._result_post_processors = list(self._result_post_processors)
        return clone

    def register(self, tool: ToolProtocol, force: bool = False) -> None:
        """Register a tool. Raises ValueError on duplicate name unless force=True.

        If the tool declares a version (via ``ToolVersioned``), it is stored
        as a specific version entry; the latest non-deprecated version is
        always exposed through ``get(name)``. Multiple versions of the same
        name can coexist.

        Contract reachability is NOT validated at registration time because
        upstream producers may not have been registered yet (plugin load
        order is non-deterministic).  Call :meth:`validate_all_contracts`
        after all tools and plugins are loaded.
        """
        name = tool.name
        info = self._extract_tool_info(tool)
        is_versioned = isinstance(tool, ToolVersioned)
        existing_versions = self._versions.get(name, {})

        if name in self._tools:
            if info.version in existing_versions:
                # Same version registered again: require force.
                if not force:
                    raise ValueError(f"Duplicate tool name: {name}")
            elif not is_versioned:
                # Unversioned tools keep the old behavior: only one registration.
                if not force:
                    raise ValueError(f"Duplicate tool name: {name}")

        self._versions.setdefault(name, {})[info.version] = tool
        self._tool_info.setdefault(name, {})[info.version] = info
        self._update_latest(name)
        self._producer_cache = None

    def get(self, name: str, version: str | None = None) -> ToolProtocol:
        """Look up a tool by name and optional version.

        When *version* is None, returns the latest non-deprecated version
        (or the latest version if all are deprecated). Raises KeyError if
        the tool or version is not found.
        """
        if version is not None:
            versions = self._versions.get(name, {})
            if version not in versions:
                raise KeyError(f"Tool {name!r} version {version!r} not found")
            return versions[version]

        if name not in self._tools:
            raise KeyError(f"Tool not found: {name}")
        return self._tools[name]

    def unregister(self, name: str, version: str | None = None) -> None:
        """Remove a tool by name.

        If *version* is provided, only that version is removed; otherwise
        all versions of the tool are removed.
        """
        if name not in self._tools:
            raise KeyError(f"Tool not found: {name}")

        if version is not None:
            versions = self._versions.get(name, {})
            if version in versions:
                del versions[version]
                infos = self._tool_info.get(name, {})
                infos.pop(version, None)
            if not versions:
                del self._versions[name]
                self._tool_info.pop(name, None)
                del self._tools[name]
            else:
                self._update_latest(name)
        else:
            del self._tools[name]
            self._versions.pop(name, None)
            self._tool_info.pop(name, None)
        self._producer_cache = None

    def list_tools(self) -> list[ToolProtocol]:
        """Return all latest-version tools."""
        return list(self._tools.values())

    @staticmethod
    def _extract_tool_info(tool: ToolProtocol) -> ToolInfo:
        """Extract version metadata from a tool instance."""
        if isinstance(tool, ToolVersioned):
            return ToolInfo(
                name=tool.name,
                version=tool.version,
                api_version=tool.api_version,
                description=tool.description,
                parameters=dict(tool.parameters),
                deprecated=tool.deprecated,
                replaced_by=tool.replaced_by,
            )
        return ToolInfo(
            name=tool.name,
            description=tool.description,
            parameters=dict(getattr(tool, "parameters", {})),
        )

    def _update_latest(self, name: str) -> None:
        """Update ``_tools[name]`` to the latest non-deprecated version."""
        versions = self._versions.get(name, {})
        if not versions:
            self._tools.pop(name, None)
            return

        sorted_versions = sorted(versions.keys())
        # Prefer latest non-deprecated version.
        for ver in reversed(sorted_versions):
            info = self._tool_info.get(name, {}).get(ver)
            if info is None or not info.deprecated:
                self._tools[name] = versions[ver]
                return
        # All versions deprecated: expose the latest anyway.
        self._tools[name] = versions[sorted_versions[-1]]

    def list_available_versions(self, name: str) -> list[str]:
        """Return all registered versions for a tool name, sorted."""
        return sorted(self._versions.get(name, {}).keys())

    def get_tool_info(self, name: str, version: str | None = None) -> ToolInfo | None:
        """Return metadata for a tool version, or None if absent."""
        infos = self._tool_info.get(name, {})
        if version is not None:
            return infos.get(version)
        # Return info for the latest version exposed by get(name).
        tool = self._tools.get(name)
        if tool is None:
            return None
        for ver, t in self._versions.get(name, {}).items():
            if t is tool:
                return infos.get(ver)
        return None

    def get_output_schema(self, name: str) -> dict | None:
        """Get the output_schema of a registered tool, or None if not declared."""
        tool = self.get(name)
        return getattr(tool, "output_schema", None)

    def get_schemas(
        self,
        *,
        hide_debug_for_task_agents: bool | None = None,
        include_deprecated: bool = True,
    ) -> list[dict[str, Any]]:
        """Return all tool schemas in OpenAI function-calling format.

        When hide_debug_for_task_agents is True, tools with runtime_policy
        hidden_from_task_agents_by_default=True are excluded.
        If None, uses the feature flag from ProjectionPolicy.
        Deprecated tools are annotated in the description and optionally
        excluded.
        """
        hide_debug = (
            hide_debug_for_task_agents
            if hide_debug_for_task_agents is not None
            else self._policy.features.hide_debug_tools_for_task_agents
        )
        schemas = []
        for name, tool in self._tools.items():
            runtime_policy = getattr(tool, "runtime_policy", None)
            if hide_debug and runtime_policy is not None:
                if getattr(runtime_policy, "hidden_from_task_agents_by_default", False):
                    continue

            info = self.get_tool_info(name)
            if info is not None and info.deprecated and not include_deprecated:
                continue

            description = tool.description
            if info is not None and info.deprecated:
                replacement = f" (use {info.replaced_by})" if info.replaced_by else ""
                description = f"[DEPRECATED{replacement}] {description}"

            schemas.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": description,
                        "parameters": _strip_host_injected_params(tool.parameters),
                    },
                }
            )
        return schemas

    async def execute(
        self,
        name: str,
        context_manager: Any | None = None,
        artifact_store: ArtifactStore | None = None,
        on_tool_start: Callable[[str], Any] | None = None,
        on_tool_progress: Callable[[str, ToolProgress], Any] | None = None,
        audit_logger: Any | None = None,
        **kwargs: Any,
    ) -> ExecutionResult:
        """Execute a tool by name with the given arguments.

        If artifact_store is provided:
          - Resolve $ref references in kwargs before execution (type-adaptive).
          - Persist successful results after execution.
          - Auto-bind contract fields from typed artifacts.
          - Register tool output as typed artifact via output_artifact_type.
        """
        try:
            tool = self.get(name)
        except KeyError:
            # Recoverable guidance instead of a bare KeyError traceback: the
            # model sees which tools ARE available and can self-correct on
            # the next turn (e.g. after hallucinating a skill name or when a
            # skill was disabled mid-session).
            available = ", ".join(sorted(t.name for t in self.list_tools())) or "(无)"
            logger.warning("Tool %r not registered; returning guidance error", name)
            return ExecutionResult.from_error(
                actor_type="tool",
                actor_name=name,
                error=render_error(
                    "errors.tool_not_found",
                    tool_name=name,
                    available_tools=available,
                ),
                metadata={"error_code": "tool_not_found"},
            )

        # --- Enforce runtime policy ---
        runtime_policy = getattr(tool, "runtime_policy", None)
        if runtime_policy is not None:
            async with self._policy_lock:
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
                logger.info("Dry-run: would auto-bind %s fields from artifacts", name)
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
        skip_resolve = getattr(tool, "skip_ref_resolution", False)
        store = artifact_store
        if store is not None and not skip_resolve:
            param_props = tool.parameters.get("properties", {})
            kwargs = store.resolve_refs(kwargs, param_props)
        elif context_manager is not None and not skip_resolve:
            kwargs = context_manager.resolve_refs(kwargs)

        # Host-injected parameters (e.g. the query embedding for hybrid
        # search): computed at the boundary, never seen or filled by the
        # model.  Runs after ref resolution so injectors see final values.
        kwargs = await self._apply_param_injectors(tool, name, kwargs)

        def on_progress(progress: ToolProgress) -> None:
            if on_tool_progress is not None:
                result = on_tool_progress(name, progress)
                if asyncio.iscoroutine(result):
                    task = asyncio.create_task(result)
                    task.add_done_callback(_log_progress_task_exception)

        try:
            raw_result = await self._execute_with_timeout(
                tool,
                name,
                on_progress=on_progress,
                context_manager=context_manager,
                artifact_store=artifact_store,
                audit_logger=audit_logger,
                **kwargs,
            )
        except asyncio.TimeoutError as exc:
            timeout = self._resolve_execution_timeout(tool)
            # An empty message is this layer's own wait_for expiry; a non-empty
            # one is an inner timeout (plugin RPC) with a more specific message.
            inner_message = str(exc)
            error = (
                inner_message
                if inner_message
                else render_error("errors.tool_timeout", timeout_seconds=timeout, tool_name=name)
            )
            logger.warning("Tool %s timed out after %ss", name, timeout)
            return await self._to_execution_result(
                name,
                ToolResult(
                    success=False,
                    error=error,
                    metadata={"blocked_reason": "tool_timeout", "timeout_seconds": timeout},
                ),
            )

        # Result post-processors run before persistence/summarization so
        # the stored artifact and the model observation see the same final
        # payload (e.g. host-side search rerank).
        for post_processor in self._result_post_processors:
            try:
                processed = post_processor(name, kwargs, raw_result)
                if asyncio.iscoroutine(processed):
                    processed = await processed
            except Exception:
                logger.warning("result post-processor failed for %s", name, exc_info=True)
            else:
                if processed is not None:
                    raw_result = processed

        # Preserve the original data before persist replaces it with a $ref marker.
        original_data = (
            raw_result.raw_data if isinstance(raw_result, ExecutionResult) else raw_result.data
        )
        output_data = original_data

        # Persist successful legacy ToolResult payloads (unless the tool opts out).
        # ExecutionResult-returning tools manage their own persistence upstream.
        # Use artifact_store for persistence — it now handles both disk I/O
        # (former CacheStore) and typed artifact registration.  When a payload
        # is persisted here, the ref is recorded in metadata as
        # ``persisted_ref_id`` so the summarizer reuses it instead of writing
        # the same content to disk a second time under a new ref_id.
        skip = getattr(tool, "skip_persist", False)
        persist_store = artifact_store
        if (
            not isinstance(raw_result, ExecutionResult)
            and persist_store is not None
            and raw_result.success
            and raw_result.data is not None
            and not skip
        ):
            tr = raw_result  # narrowed to ToolResult for static analysis
            if isinstance(tr.data, dict) and tr.data.get("__persisted_output__"):
                pass
            else:
                source_ref_id = tr.metadata.get("source_ref_id")
                source_query = tr.metadata.get("source_query")
                label = tr.metadata.get("label")
                persist_result = await persist_store.persist(
                    tr.data,
                    tool.name,
                    tool_registry=self,
                    source_ref_id=source_ref_id,
                    source_query=source_query,
                    label=label,
                )
                output_data = persist_result.data
                if persist_result.persisted:
                    raw_result = tr.model_copy(
                        update={
                            "metadata": {
                                **tr.metadata,
                                "persisted_ref_id": persist_result.ref_id,
                            }
                        }
                    )

        metadata_updates: dict[str, Any] = {}
        if artifact_bindings:
            metadata_updates["artifact_bindings"] = artifact_bindings

        if metadata_updates:
            if isinstance(raw_result, ExecutionResult):
                from dataclasses import replace

                raw_result = replace(
                    raw_result, metadata={**raw_result.metadata, **metadata_updates}
                )
            else:
                raw_result = raw_result.model_copy(
                    update={"metadata": {**raw_result.metadata, **metadata_updates}}
                )

        # Single-track normalization: ``_to_execution_result`` is the one
        # conversion point for legacy ToolResults; everything below handles
        # only ExecutionResult.
        result = await self._to_execution_result(
            name,
            raw_result,
            original_data=original_data,
            skip_summarize=getattr(tool, "skip_summarize", False),
        )

        # Register tool output as typed artifact.
        # ArtifactStore now handles both disk persistence AND typed registration
        # — no separate cache_store bridge needed.  Tool outputs are automatically
        # registered even when the tool doesn't declare output_artifact_type.
        if artifact_store is not None and result.success:
            try:
                skip_artifact_registration = getattr(tool, "skip_artifact_registration", False)
                if not skip_artifact_registration:
                    output_artifact_type = getattr(tool, "output_artifact_type", None)
                    if output_artifact_type:
                        self._register_output_artifact(
                            tool_name=name,
                            artifact_type=output_artifact_type,
                            data=output_data,
                            artifact_store=artifact_store,
                            persisted_ref_id=getattr(result, "result_id", None)
                            or (result.metadata or {}).get("persisted_ref_id"),
                        )
                    else:
                        # Auto-register with derived type — no tool left invisible.
                        derived_type = self._derive_artifact_type(tool_name=name)
                        self._register_output_artifact(
                            tool_name=name,
                            artifact_type=derived_type,
                            data=output_data,
                            artifact_store=artifact_store,
                            debug_only=derived_type == "core.cached_output",
                            persisted_ref_id=getattr(result, "result_id", None)
                            or (result.metadata or {}).get("persisted_ref_id"),
                        )
            except Exception:
                logger.warning(
                    "Failed to register output artifact for %s",
                    name,
                    exc_info=True,
                )

        return result

    def _resolve_execution_timeout(self, tool: ToolProtocol) -> float:
        """Effective execution timeout: tool override first, then settings.

        ``execution_timeout`` <= 0 on the tool disables the wrapper (for tools
        that manage their own deadline, e.g. SkillTool via the runtime budget).
        """
        override = getattr(tool, "execution_timeout", None)
        if override is not None:
            return float(override)
        from courtier.config import get_settings

        return float(getattr(get_settings(), "tool_timeout_seconds", 0.0) or 0.0)

    async def _execute_with_timeout(
        self,
        tool: ToolProtocol,
        name: str,
        **execute_kwargs: Any,
    ) -> Any:
        """Run tool.execute() under the effective timeout (0 = unlimited)."""
        timeout = self._resolve_execution_timeout(tool)
        if timeout <= 0:
            return await tool.execute(**execute_kwargs)
        return await asyncio.wait_for(tool.execute(**execute_kwargs), timeout=timeout)

    def _check_runtime_policy(
        self, name: str, policy: RuntimePolicy, kwargs: dict[str, Any] | None = None
    ) -> ToolResult | None:
        """Enforce runtime policy. Returns ToolResult if blocked, None if allowed."""
        self._tool_call_counts[name] = self._tool_call_counts.get(name, 0) + 1
        if self._last_tool_called == name:
            self._tool_consecutive_counts[name] = self._tool_consecutive_counts.get(name, 0) + 1
        else:
            self._tool_consecutive_counts[name] = 1
        self._last_tool_called = name

        max_calls = policy.max_calls
        max_consecutive = policy.max_consecutive

        if max_calls is not None and self._tool_call_counts[name] > max_calls:
            emit_event(
                "repeated_tool_call_blocked",
                {
                    "tool": name,
                    "reason": "max_calls",
                    "count": self._tool_call_counts[name],
                    "limit": max_calls,
                },
            )
            return ToolResult(
                success=False,
                error=render_error(
                    "errors.tool_max_calls_exceeded",
                    tool_name=name,
                    count=self._tool_call_counts[name],
                    limit=max_calls,
                ),
                metadata={"blocked_reason": "max_calls_exceeded"},
            )
        current_consecutive = self._tool_consecutive_counts.get(name, 0)
        if max_consecutive is not None and current_consecutive > max_consecutive:
            emit_event(
                "repeated_tool_call_blocked",
                {
                    "tool": name,
                    "reason": "max_consecutive_calls",
                    "count": current_consecutive,
                    "limit": max_consecutive,
                },
            )
            return ToolResult(
                success=False,
                error=render_error(
                    "errors.tool_max_consecutive_exceeded",
                    tool_name=name,
                    count=current_consecutive,
                    limit=max_consecutive,
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
            fields,
            tool_name,
            explicit_kwargs,
            producers=producers,
        )

    def _resolve_persisted_data(self, data: Any) -> Any | None:
        """Load real data when *data* is a ``__persisted_output__`` marker.

        Returns the original *data* if it is not a persisted marker.  Returns
        ``None`` when the referenced cache file cannot be read so callers can
        skip registering a stale marker as an artifact.
        """
        import json
        from pathlib import Path

        if not isinstance(data, dict) or not data.get("__persisted_output__"):
            return data
        filepath = data.get("file")
        if not filepath:
            return data
        try:
            return json.loads(Path(filepath).read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load persisted output from %s: %s", filepath, exc)
            return None

    def _register_output_artifact(
        self,
        *,
        tool_name: str,
        artifact_type: str,
        data: Any,
        artifact_store: ArtifactStore,
        debug_only: bool = False,
        persisted_ref_id: str | None = None,
    ) -> None:
        """Register a tool output as an artifact via ``register_cached_ref``.

        Single path for both tools that declare ``output_artifact_type`` and
        the auto-registration fallback with a derived type; the fallback
        passes ``debug_only=True`` for the generic ``core.cached_output`` type.

        The typed artifact id is aligned with the persistence layer: when the
        output was persisted — by the registry-level persist or by the
        summarizer, whose ``ExecutionResult.result_id`` carries the numbered
        ``$ref:<tool>:N`` — the artifact is registered under that SAME id so
        get_artifact's persisted-ref read and typed projection resolve against
        one id space.  Unpersisted (small, inline) outputs get a
        content-addressed ``inline:<tool>:<hash>`` id.
        """
        if data is None:
            return
        # Unpersisted (small, inline) outputs get a content-addressed id
        # instead of the single-slot ``$ref:<tool>:latest`` — repeated calls
        # must not overwrite each other's artifact, and the id must not
        # masquerade as a persistence ref.
        ref_id = persisted_ref_id or f"inline:{tool_name}:{stable_content_hash(data)[-12:]}"
        if isinstance(data, dict) and data.get("__persisted_output__"):
            ref_id = data.get("ref_id", ref_id)
            data = self._resolve_persisted_data(data)
            if data is None:
                return
        # Artifacts are projection-allowed so downstream tools
        # can discover them via list_artifacts.
        artifact_store.register_cached_ref(
            ref_id=ref_id,
            artifact_type=artifact_type,
            created_by=tool_name,
            data=data,
            role="intermediate",
            subject="unknown",
            projection_allowed=True,
            debug_only=debug_only,
        )
        emit_event(
            "artifact_created",
            {
                "artifact_type": artifact_type,
                "created_by": tool_name,
                "ref_id": ref_id,
            },
        )

    # -- Artifact type derivation -----------------------------------------------

    # Centralized tool-name → artifact-type mapping.  Tools that declare
    # ``output_artifact_type`` take precedence; this mapping provides sensible
    # defaults for tools that don't.
    _TOOL_ARTIFACT_TYPE: dict[str, str] = {
        "parse_layout": "docaudit.parsed_layout",
    }

    @classmethod
    def _derive_artifact_type(cls, *, tool_name: str) -> str:
        """Return the artifact type for *tool_name*.

        Falls back to ``"core.cached_output"`` when no mapping exists so that
        every tool output is at least visible in ``list_artifacts``.
        """
        return cls._TOOL_ARTIFACT_TYPE.get(tool_name, "core.cached_output")

    def configure_result_handling(
        self,
        *,
        result_store: Any | None = None,
        summarizer: Any | None = None,
    ) -> None:
        """Attach result-store/summarizer used to wrap ToolResults into ExecutionResults."""
        self._result_store = result_store
        self._summarizer = summarizer

    def configure_param_injectors(self, injectors: dict[str, Any]) -> None:
        """Register host-side parameter injectors by injector name.

        Keys match the ``x-host-injected`` marker value on tool schema
        properties (see courtier.agent.tools.param_injection)."""
        self._param_injectors = dict(injectors)

    def register_param_injectors(self, injectors: dict[str, Any]) -> None:
        """Merge additional injectors into the existing set (session clones
        extend the app-wide base — e.g. the per-session memory caller
        identity — without replacing inherited injectors)."""
        merged = dict(self._param_injectors)
        merged.update(injectors)
        self._param_injectors = merged

    def configure_result_post_processors(self, processors: list[ResultPostProcessor]) -> None:
        """Register result post-processors run between tool.execute and persistence."""
        self._result_post_processors = list(processors)

    async def _apply_param_injectors(
        self, tool: ToolProtocol, name: str, kwargs: dict[str, Any]
    ) -> Any:
        """Await all host-injected parameters the tool contract declares.

        Host-injected params are never model-fillable: a hallucinated (or
        prompt-injected) value is stripped before injection so the host's
        result is the only thing the tool ever sees.  Returns the kwargs
        mapping to execute with (a new dict when anything was injected or
        stripped, else the original).  A failing injector only skips its
        own parameter.
        """
        if not self._param_injectors:
            # No injectors registered: still strip model-supplied
            # host-injected params — passing them through would let the
            # model forge identity (_caller_uid/_caller_is_admin).
            properties = tool.parameters.get("properties", {})
            forged = {
                name
                for name, prop in properties.items()
                if isinstance(prop, dict) and prop.get(HOST_INJECTED_MARKER)
            }
            if forged and forged & kwargs.keys():
                return {k: v for k, v in kwargs.items() if k not in forged}
            return kwargs
        properties = tool.parameters.get("properties", {})
        updates: dict[str, Any] = {}
        stripped: set[str] = set()
        for param_name, prop in properties.items():
            injector_name = prop.get(HOST_INJECTED_MARKER) if isinstance(prop, dict) else None
            if not injector_name:
                continue
            if param_name in kwargs:
                stripped.add(param_name)
            injector = self._param_injectors.get(injector_name)
            if injector is None:
                continue
            try:
                injected = injector(tool, kwargs, param_name)
                if asyncio.iscoroutine(injected):
                    injected = await injected
            except Exception:
                logger.warning(
                    "param injector %r failed for %s.%s",
                    injector_name,
                    name,
                    param_name,
                    exc_info=True,
                )
                continue
            if injected is not None:
                updates[param_name] = injected
        if not updates and not stripped:
            return kwargs
        return {**{k: v for k, v in kwargs.items() if k not in stripped}, **updates}

    def _get_producers(self) -> dict[str, list[str]]:
        """返回缓存的 producers 映射，延迟计算."""
        if self._producer_cache is None:
            self._producer_cache = derive_upstream_producers(
                list(self._tools.values()),
                self._projector_registry,
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

        *original_data* is the data before the artifact store replaced it with a
        $ref marker; it is used for summarization so the parent agent sees content
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
            return cast(
                ExecutionResult,
                await self._summarizer.from_data(
                    success=tool_result.success,
                    actor_type="tool",
                    actor_name=tool_name,
                    data=data,
                    error=tool_result.error,
                    metadata=metadata,
                ),
            )
        if not tool_result.success:
            return ExecutionResult.from_error(
                actor_type="tool",
                actor_name=tool_name,
                error=tool_result.error
                or render_error("errors.result_unknown_error", actor_name=tool_name),
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
