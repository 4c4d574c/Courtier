# Plugin-Artifact-Agent Loop Interaction Fixes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix 8 plugin-agent-artifact interaction bugs and simplify contract models from 5 Pydantic models (20+ fields) to 2 dataclasses (5 fields).

**Architecture:** Foundation-first: simplify artifact contract models, then propagate them through ProxyTool/ProxyAgent, fix streaming in PluginRuntime, and patch JSON-RPC race condition. Dual-path strategy (new InputField + legacy ToolInputContract) avoids rewriting the resolver in one pass.

**Tech Stack:** Python 3.10+, Pydantic, asyncio, JSON-RPC over stdio

**Spec:** `docs/superpowers/specs/2026-06-08-plugin-artifact-interaction-fixes-design.md`

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `src/agent/artifacts/models.py` | Modify | Add InputField, RuntimePolicy dataclasses |
| `src/agent/artifacts/resolver.py` | Modify | Simplify _artifact_allowed() |
| `src/agent/artifacts/binder.py` | Modify | Accept InputField alongside ToolInputContract |
| `src/agent/tools/registry.py` | Modify | Dual-path auto-bind, output reg, runtime policy |
| `src/agent/core/loop_hints.py` | Modify | Check input_fields alongside input_contract |
| `src/plugin/proxies.py` | Modify | #1 Complete _HOST_KWARGS, #2 read contracts from spec, #6 skip_persist |
| `src/plugin/registry.py` | Modify | #7 Warning on tool name collision |
| `src/agent/agents/base.py` | Modify | #7 Incremental sync from shared registry |
| `src/plugin/client.py` | Modify | #5 _closed check in stream() loop |
| `src/plugin/sdk/runtime.py` | Modify | #3 Real streaming + register_tool() |
| `plugins/parse/tools.py` | Modify | Replace output_contract → output_artifact_type |
| `plugins/search/tools.py` | Modify | Replace output_contract → output_artifact_type |
| `plugins/plagiarism/tools.py` | Modify | Replace input_contract → input_fields, output_contract → output_artifact_type |
| `plugins/content_audit/tools.py` | Modify | Replace input_contract → input_fields |
| `plugins/format_audit/tools.py` | Modify | Replace input_contract → input_fields |
| `plugins/text_correction/tools.py` | Modify | Replace input_contract → input_fields |
| `src/agent/tools/builtin/list_artifacts.py` | Modify | Replace output_contract → output_artifact_type, runtime_policy → RuntimePolicy |
| `src/agent/tools/builtin/get_artifact.py` | Modify | Replace output_contract → output_artifact_type, runtime_policy → RuntimePolicy, dynamic input_contract |
| `plugins/*/entry.py` (9 files) | Modify | Use register_tool() |

---

## Phase 0: Contract Model Simplification (Foundation)

### Task 0.1: Add InputField and RuntimePolicy to models.py

**Files:**
- Modify: `src/agent/artifacts/models.py`

- [ ] **Step 1: Add new dataclasses at end of file (before `stable_content_hash`)**

Open `src/agent/artifacts/models.py`. Add after the `upstream_producer_for` function (line ~556) and before `stable_content_hash` (line ~591):

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class InputField:
    """A single input field requirement for a tool.

    Declares that the tool's parameter ``name`` should be auto-bound
    from an artifact of ``artifact_type``.

    Compared to the legacy ``ContractField``, this removes role/subject
    matching (always default values in practice) and constraint/sensitivity
    gates (never used).
    """

    name: str
    artifact_type: str
    materialize_as: str | None = None  # derived from artifact_type if None


@dataclass(frozen=True)
class RuntimePolicy:
    """Runtime safety policy for a tool.

    Compared to the legacy ``ToolRuntimePolicy``, this removes:
    - allow_chaining (never set to False)
    - hidden_from_task_agents_by_default (handled by ProjectionPolicy)
    - require_debug_policy (never set to True)
    """

    max_calls: int | None = None
    max_consecutive: int | None = None
```

- [ ] **Step 2: Run existing tests to verify nothing breaks**

```bash
uv run pytest tests/agent/test_registry.py tests/agent/test_loop.py -x -q
```

Expected: All pass (new classes are unused, no impact).

- [ ] **Step 3: Commit**

```bash
git add src/agent/artifacts/models.py
git commit -m "feat: add InputField and RuntimePolicy dataclasses to artifacts/models.py"
```

---

### Task 0.2: Simplify resolver._artifact_allowed()

**Files:**
- Modify: `src/agent/artifacts/resolver.py:238-264`

- [ ] **Step 1: Replace the _artifact_allowed method**

In `src/agent/artifacts/resolver.py`, replace lines 238-264:

```python
# Before
@staticmethod
def _artifact_allowed(
    artifact: Artifact,
    field: ContractField,
    policy: ProjectionPolicy,
) -> bool:
    if artifact.metadata.debug_only and not policy.allow_debug_artifacts:
        return False
    if not artifact.metadata.projection_allowed and not policy.allow_debug_artifacts:
        return False
    if artifact.metadata.semantic_role != field.role:
        return False
    if artifact.metadata.subject != field.subject:
        return False
    # Scope constraint
    field_scope = field.constraints.get("source_scope")
    if field_scope and artifact.metadata.scope not in ("current_task", ""):
        if artifact.metadata.scope != field_scope:
            return False
    # Sensitivity gate
    if artifact.metadata.sensitivity not in ("internal", ""):
        allowed_sensitivity = field.constraints.get("allowed_sensitivity", "internal")
        if artifact.metadata.sensitivity != allowed_sensitivity:
            return False
    return True

# After
@staticmethod
def _artifact_allowed(
    artifact: Artifact,
    field: ContractField,
    policy: ProjectionPolicy,
) -> bool:
    """Check if artifact can satisfy a contract field.

    Simplified: only checks debug_only and projection_allowed flags.
    Role/subject/scope/sensitivity matching removed — all artifacts
    in a single-run session share the same role/subject defaults,
    so the extra checks never rejected anything in practice.
    """
    if artifact.metadata.debug_only and not policy.allow_debug_artifacts:
        return False
    if not artifact.metadata.projection_allowed:
        return False
    return True
```

- [ ] **Step 2: Run resolver tests**

```bash
uv run pytest tests/agent/ -x -q -k "artifact or resolver or binder" 2>/dev/null || echo "No specific tests — run full suite"
uv run pytest tests/ -x -q --ignore=tests/plugin
```

Expected: All pass. The removed checks never rejected any artifact in current tests.

- [ ] **Step 3: Commit**

```bash
git add src/agent/artifacts/resolver.py
git commit -m "refactor: simplify resolver._artifact_allowed() — remove unused role/subject/scope/sensitivity gates"
```

---

### Task 0.3: Add synthetic contract bridge to ToolRegistry

**Files:**
- Modify: `src/agent/tools/registry.py`

- [ ] **Step 1: Update imports**

At top of `src/agent/tools/registry.py`, add to existing import from `src.agent.artifacts.models`:

```python
# Change line 10 from:
from src.agent.artifacts.models import ProjectionPolicy, ToolRuntimePolicy
# To:
from src.agent.artifacts.models import (
    ProjectionPolicy, ToolRuntimePolicy, InputField, RuntimePolicy,
)
```

- [ ] **Step 2: Update auto-binding check in execute()**

Replace lines 131-150 (`input_contract = getattr(tool, "input_contract", None)` through the binding block) with dual-path:

```python
        # Auto-bind contract arguments from typed artifacts
        artifact_bindings: dict[str, str] = {}
        input_fields = getattr(tool, "input_fields", None)
        input_contract = getattr(tool, "input_contract", None)

        if (
            artifact_store is not None
            and self._policy.features.tool_auto_binding_enabled
        ):
            effective_contract = None

            # New path: input_fields (simplified)
            if input_fields is not None and input_fields:
                from src.agent.artifacts.models import ContractField
                synthetic = ToolInputContract(
                    tool_name=name,
                    fields=tuple(
                        ContractField(
                            name=f.name,
                            artifact_type=f.artifact_type,
                            role="primary_document",
                            subject="current_upload",
                            materialize_as=f.materialize_as or "pass_through",
                        )
                        for f in input_fields
                    ),
                )
                effective_contract = synthetic

            # Legacy path: input_contract
            elif input_contract is not None:
                effective_contract = input_contract

            if effective_contract is not None:
                if self._policy.features.resolver_dry_run:
                    logger.info(
                        "Dry-run: would auto-bind %s fields from artifacts",
                        effective_contract.tool_name,
                    )
                else:
                    binding_result = self._bind_contract_arguments(
                        input_contract=effective_contract,
                        artifact_store=artifact_store,
                        explicit_kwargs=kwargs,
                    )
                    if not binding_result.success:
                        return binding_result
                    kwargs = {**kwargs, **binding_result.data["arguments"]}
                    artifact_bindings = binding_result.data["artifact_bindings"]
```

- [ ] **Step 3: Update runtime policy check**

Replace lines 126-128:

```python
        # --- Enforce ToolRuntimePolicy ---
        runtime_policy = getattr(tool, "runtime_policy", None)
        if isinstance(runtime_policy, ToolRuntimePolicy):
            refused = self._check_runtime_policy(name, runtime_policy, kwargs)
            if refused is not None:
                return refused
```

With dual-path:

```python
        # --- Enforce RuntimePolicy / ToolRuntimePolicy ---
        runtime_policy = getattr(tool, "runtime_policy", None)
        if runtime_policy is not None:
            refused = self._check_runtime_policy(name, runtime_policy, kwargs)
            if refused is not None:
                return refused
```

- [ ] **Step 4: Update _check_runtime_policy to accept both types**

Replace `_check_runtime_policy` (lines 215-266) to handle both `RuntimePolicy` and `ToolRuntimePolicy`:

```python
    def _check_runtime_policy(
        self, name: str, policy: Any, kwargs: dict[str, Any] | None = None
    ) -> ToolResult | None:
        """Enforce RuntimePolicy or ToolRuntimePolicy. Returns ToolResult if blocked."""
        # Determine max_calls and max_consecutive from either policy type
        if isinstance(policy, RuntimePolicy):
            max_calls = policy.max_calls
            max_consecutive = policy.max_consecutive
        elif isinstance(policy, ToolRuntimePolicy):
            max_calls = policy.max_calls_per_agent_run
            max_consecutive = policy.max_consecutive_calls
        else:
            return None

        # Track call counts
        self._tool_call_counts[name] = self._tool_call_counts.get(name, 0) + 1
        if self._last_tool_called == name:
            self._tool_consecutive_counts[name] = (
                self._tool_consecutive_counts.get(name, 0) + 1
            )
        else:
            self._tool_consecutive_counts[name] = 1
        self._last_tool_called = name

        if (
            max_calls is not None
            and self._tool_call_counts[name] > max_calls
        ):
            emit_event("repeated_tool_call_blocked", {
                "tool": name,
                "reason": "max_calls_per_agent_run",
                "count": self._tool_call_counts[name],
                "limit": max_calls,
            })
            return ToolResult(
                success=False,
                error=(
                    f"Tool '{name}' has been called {self._tool_call_counts[name]} times, "
                    f"exceeding the limit of {max_calls}."
                ),
                metadata={"blocked_reason": "max_calls_exceeded"},
            )
        if (
            max_consecutive is not None
            and self._tool_consecutive_counts.get(name, 0) > max_consecutive
        ):
            emit_event("repeated_tool_call_blocked", {
                "tool": name,
                "reason": "max_consecutive_calls",
                "count": self._tool_consecutive_counts[name],
                "limit": max_consecutive,
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
```

- [ ] **Step 5: Update output artifact registration (dual-path)**

Replace lines 200-211:

```python
        # Register tool output as typed artifact via output_contract
        output_artifact_type = getattr(tool, "output_artifact_type", None)
        output_contract = getattr(tool, "output_contract", None)
        if artifact_store is not None and result.success:
            if output_artifact_type is not None:
                try:
                    self._register_output_artifact_simple(
                        tool_name=name,
                        artifact_type=output_artifact_type,
                        result=result,
                        artifact_store=artifact_store,
                    )
                except Exception:
                    logger.debug(
                        "Failed to register output artifact for %s", name, exc_info=True
                    )
            elif output_contract is not None:
                try:
                    self._register_output_artifact(
                        tool_name=name,
                        output_contract=output_contract,
                        result=result,
                        artifact_store=artifact_store,
                    )
                except Exception:
                    logger.debug(
                        "Failed to register output artifact for %s", name, exc_info=True
                    )
```

- [ ] **Step 6: Add _register_output_artifact_simple method**

Add after `_register_output_artifact` (after line ~292):

```python
    def _register_output_artifact_simple(
        self,
        *,
        tool_name: str,
        artifact_type: str,
        result: ToolResult,
        artifact_store: ArtifactStore,
    ) -> None:
        """Register tool output as a typed artifact using simplified contract."""
        import json
        from pathlib import Path

        if not result.success or result.data is None:
            return
        data = result.data
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
            ref_id=ref_id,
            artifact_type=artifact_type,
            created_by=tool_name,
            data=data,
            role="intermediate",
            subject="unknown",
            projection_allowed=True,
            debug_only=False,
        )
        emit_event("artifact_created", {
            "artifact_type": artifact_type,
            "created_by": tool_name,
            "ref_id": ref_id,
        })
```

- [ ] **Step 7: Run tests**

```bash
uv run pytest tests/agent/test_registry.py -x -q
```

Expected: All pass.

- [ ] **Step 8: Commit**

```bash
git add src/agent/tools/registry.py
git commit -m "feat: add dual-path contract support to ToolRegistry (InputField + legacy)"
```

---

### Task 0.4: Update loop_hints.py for input_fields

**Files:**
- Modify: `src/agent/core/loop_hints.py`

- [ ] **Step 1: Add helper to build synthetic contract from input_fields**

Add this helper function at the top of `loop_hints.py` (after imports, before `_build_blocked_tools_hints`):

```python
def _get_effective_contract(tool: Any, artifact_store: Any) -> Any | None:
    """Return a ToolInputContract for *tool*, building from input_fields if needed.

    Returns None if the tool has no contract at all (not a terminal tool).
    """
    input_fields = getattr(tool, "input_fields", None)
    input_contract = getattr(tool, "input_contract", None)

    if input_fields is not None and input_fields:
        from src.agent.artifacts.models import ContractField, ToolInputContract
        return ToolInputContract(
            tool_name=tool.name,
            fields=tuple(
                ContractField(
                    name=f.name,
                    artifact_type=f.artifact_type,
                    role="primary_document",
                    subject="current_upload",
                    materialize_as=f.materialize_as or "pass_through",
                )
                for f in input_fields
            ),
        )
    if input_contract is not None and input_contract.require_contract_binding:
        return input_contract
    return None
```

- [ ] **Step 2: Update _build_blocked_tools_hints (line 36-56)**

Replace lines 36-57:

```python
    lines: list[str] = []
    for tool in tool_registry.list_tools():
        input_contract = getattr(tool, "input_contract", None)
        if input_contract is None or not input_contract.require_contract_binding:
            continue
        candidates = artifact_store.list_projection_candidates()
        resolver = ProjectionResolver(create_default_projector_registry())
        resolution = resolver.resolve(input_contract, candidates, ProjectionPolicy())
```

With:

```python
    lines: list[str] = []
    for tool in tool_registry.list_tools():
        effective = _get_effective_contract(tool, artifact_store)
        if effective is None:
            continue
        candidates = artifact_store.list_projection_candidates()
        resolver = ProjectionResolver(create_default_projector_registry())
        resolution = resolver.resolve(effective, candidates, ProjectionPolicy())
```

And replace all references to `input_contract.tool_name` with `effective.tool_name` in the rest of the function (lines 44, 52).

- [ ] **Step 3: Update _is_terminal_tool_ready (line 59-84)**

Replace lines 73-83:

```python
    for tool in tool_registry.list_tools():
        input_contract = getattr(tool, "input_contract", None)
        if input_contract is None or not input_contract.require_contract_binding:
            continue
        candidates = artifact_store.list_projection_candidates()
        if not candidates:
            continue
        resolver = ProjectionResolver(create_default_projector_registry())
        resolution = resolver.resolve(input_contract, candidates, ProjectionPolicy())
```

With:

```python
    for tool in tool_registry.list_tools():
        effective = _get_effective_contract(tool, artifact_store)
        if effective is None:
            continue
        candidates = artifact_store.list_projection_candidates()
        if not candidates:
            continue
        resolver = ProjectionResolver(create_default_projector_registry())
        resolution = resolver.resolve(effective, candidates, ProjectionPolicy())
```

- [ ] **Step 4: Update _get_ready_terminal_tools (line 87-109)**

Same pattern — replace lines 98-108 with the `_get_effective_contract` pattern. Replace `ready.append(input_contract.tool_name)` with `ready.append(effective.tool_name)`.

- [ ] **Step 5: Update _build_terminal_ready_hints (line 112-148)**

Same pattern — replace lines 125-148 with `_get_effective_contract` pattern. Replace `input_contract.tool_name` references with `effective.tool_name`.

- [ ] **Step 6: Run tests**

```bash
uv run pytest tests/agent/test_loop.py -x -q
```

Expected: All pass.

- [ ] **Step 7: Commit**

```bash
git add src/agent/core/loop_hints.py
git commit -m "refactor: update loop_hints.py to check input_fields alongside input_contract"
```

---

### Task 0.5: Migrate host-side tool classes to simplified contracts

**Files:**
- Modify: `plugins/parse/tools.py`
- Modify: `plugins/search/tools.py`
- Modify: `plugins/plagiarism/tools.py`
- Modify: `plugins/content_audit/tools.py`
- Modify: `plugins/format_audit/tools.py`
- Modify: `plugins/text_correction/tools.py`
- Modify: `src/agent/tools/builtin/list_artifacts.py`
- Modify: `src/agent/tools/builtin/get_artifact.py`

- [ ] **Step 1: Migrate ParseTool (`plugins/parse/tools.py`)**

Replace:
```python
from src.agent.artifacts.models import OutputArtifactContract, ToolOutputContract
```
With:
```python
# (contract types no longer needed for output; InputField/RuntimePolicy imported if needed)
```

Replace lines 82-91 (output_contract) with:
```python
    output_artifact_type: str | None = "docaudit.parsed_document"
```

- [ ] **Step 2: Migrate SearchDocumentsTool (`plugins/search/tools.py`)**

Replace:
```python
from src.agent.artifacts.models import (
    OutputArtifactContract,
    ToolOutputContract,
)
```

Replace `output_contract = ToolOutputContract(...)` with:
```python
    output_artifact_type: str | None = "docaudit.search_results"
```

- [ ] **Step 3: Migrate DetectPlagiarismTool (`plugins/plagiarism/tools.py`)**

Replace:
```python
from src.agent.artifacts.models import (
    ContractField,
    ToolInputContract,
    OutputArtifactContract,
    ToolOutputContract,
)
```
With:
```python
from src.agent.artifacts.models import InputField
```

Replace `input_contract` with:
```python
    input_fields: tuple[InputField, ...] = (
        InputField(
            name="reference_texts",
            artifact_type="docaudit.reference_text_list",
        ),
        InputField(
            name="document_text",
            artifact_type="core.text_collection",
        ),
    )
```

Replace `output_contract` with:
```python
    output_artifact_type: str | None = "docaudit.plagiarism_report"
```

- [ ] **Step 4: Migrate AuditContentTool (`plugins/content_audit/tools.py`)**

Replace:
```python
from src.agent.artifacts.models import (
    ContractField,
    ToolInputContract,
)
```
With:
```python
from src.agent.artifacts.models import InputField
```

Replace `input_contract` with:
```python
    input_fields: tuple[InputField, ...] = (
        InputField(
            name="paragraphs",
            artifact_type="docaudit.paragraph_list",
        ),
    )
```

- [ ] **Step 5: Migrate AuditFormatTool (`plugins/format_audit/tools.py`)**

Replace:
```python
from src.agent.artifacts.models import (
    ContractField,
    ToolInputContract,
)
```
With:
```python
from src.agent.artifacts.models import InputField
```

Replace `input_contract` with:
```python
    input_fields: tuple[InputField, ...] = (
        InputField(
            name="document",
            artifact_type="docaudit.parsed_document",
        ),
    )
```

- [ ] **Step 6: Migrate CorrectTextTool (`plugins/text_correction/tools.py`)**

Replace:
```python
from src.agent.artifacts.models import (
    ContractField,
    ToolInputContract,
)
```
With:
```python
from src.agent.artifacts.models import InputField
```

Replace `input_contract` with:
```python
    input_fields: tuple[InputField, ...] = (
        InputField(
            name="paragraphs",
            artifact_type="docaudit.paragraph_list",
        ),
    )
```

- [ ] **Step 7: Migrate ListArtifactsTool (`src/agent/tools/builtin/list_artifacts.py`)**

Replace:
```python
from src.agent.artifacts.models import (
    OutputArtifactContract,
    ToolOutputContract,
    ToolRuntimePolicy,
)
```
With:
```python
from src.agent.artifacts.models import RuntimePolicy
```

Replace:
```python
    runtime_policy = ToolRuntimePolicy(
        max_calls_per_agent_run=5,
        max_consecutive_calls=1,
    )
```
With:
```python
    runtime_policy = RuntimePolicy(
        max_calls=5,
        max_consecutive=1,
    )
```

Replace `output_contract = ToolOutputContract(...)` with:
```python
    output_artifact_type: str | None = "core.debug_view"
```

- [ ] **Step 8: Migrate GetArtifactTool (`src/agent/tools/builtin/get_artifact.py`)**

Replace imports as in Step 7. Replace `runtime_policy` and `output_contract` as in Step 7 (`output_artifact_type = "core.debug_view"`). The dynamic `ToolInputContract` creation in execute() stays as legacy — it's runtime-constructed.

- [ ] **Step 9: Run all tests**

```bash
uv run pytest tests/ -x -q --ignore=tests/plugin
```

Expected: All pass (tools still work via legacy path in ToolRegistry).

- [ ] **Step 10: Commit**

```bash
git add plugins/parse/tools.py plugins/search/tools.py plugins/plagiarism/tools.py \
        plugins/content_audit/tools.py plugins/format_audit/tools.py \
        plugins/text_correction/tools.py \
        src/agent/tools/builtin/list_artifacts.py src/agent/tools/builtin/get_artifact.py
git commit -m "refactor: migrate tool classes to simplified contracts (InputField, output_artifact_type, RuntimePolicy)"
```

---

## Phase 1: ProxyTool/ProxyAgent Fixes (#1, #2, #6, #7, #8)

### Task 1.1: Fix ProxyAgent._HOST_KWARGS (#1) and ProxyTool contract propagation (#2, #6)

**Files:**
- Modify: `src/plugin/proxies.py`

- [ ] **Step 1: Complete ProxyAgent._HOST_KWARGS**

In `src/plugin/proxies.py`, line 105, replace:

```python
    _HOST_KWARGS = frozenset({"input", "context", "state"})
```

With:

```python
    _HOST_KWARGS = frozenset({
        "input", "context", "state",
        "artifact_store", "context_manager", "audit_logger",
        "on_step", "on_token", "on_content_token", "on_tool_result",
    })
```

- [ ] **Step 2: Update ProxyTool.__init__ to read contract fields from tool_spec**

Replace `ProxyTool.__init__` (lines 23-36):

```python
    def __init__(self, client: Any, tool_spec: dict[str, Any]) -> None:
        self._client = client
        self.name: str = tool_spec["name"]
        self.display_name: str | None = tool_spec.get("display_name")
        self.description: str = tool_spec.get("description", "")
        self.parameters: dict[str, Any] = tool_spec.get("parameters", {})

        # Contract fields — read from plugin spec if present (#2)
        self.output_artifact_type: str | None = tool_spec.get("output_artifact_type")
        self.input_fields: tuple = tuple(
            InputField(**f) for f in tool_spec.get("input_fields", [])
        ) if "input_fields" in tool_spec else ()
        self.output_schema: dict | None = tool_spec.get("output_schema")
        self.skip_persist: bool = tool_spec.get("skip_persist", False)  # (#6)
        self.skip_ref_resolution: bool = tool_spec.get("skip_ref_resolution", False)

        rp = tool_spec.get("runtime_policy")
        self.runtime_policy: RuntimePolicy | None = (
            RuntimePolicy(**rp) if rp else None
        )

        # Legacy compat — kept as None so dual-path code doesn't try to use them
        self.input_contract: Any | None = None
        self.output_contract: Any | None = None
```

Add the InputField and RuntimePolicy imports at the top of the file:

```python
from src.agent.artifacts.models import InputField, RuntimePolicy
```

- [ ] **Step 3: Run proxy tests**

```bash
uv run pytest tests/plugin/test_proxies.py -x -q
```

Expected: All pass (MockClient doesn't use _HOST_KWARGS path, and new fields default gracefully).

- [ ] **Step 4: Commit**

```bash
git add src/plugin/proxies.py
git commit -m "fix: complete ProxyAgent._HOST_KWARGS and propagate contract fields to ProxyTool

- #1: Add artifact_store, context_manager, audit_logger, and on_* callbacks
      to ProxyAgent._HOST_KWARGS to prevent JSON serialization crashes
- #2: Read output_artifact_type, input_fields, output_schema, runtime_policy
      from tool_spec in ProxyTool.__init__
- #6: Read skip_persist and skip_ref_resolution from tool_spec"
```

---

### Task 1.2: Add collision warning in ExtensionRegistry (#7)

**Files:**
- Modify: `src/plugin/registry.py`

- [ ] **Step 1: Add warning on tool name collision**

In `_register_tool` (lines 135-148), add warning before the overwrite:

```python
    def _register_tool(self, plugin_name: str, client: JSONRPCClient, cap: dict) -> None:
        if self._tool_registry is None:
            return
        proxy = ProxyTool(client, cap)
        try:
            self._tool_registry.register(proxy)
        except ValueError:
            logger.warning(
                "Plugin '%s' tool '%s' overwrites existing tool '%s'. "
                "If the host tool had artifact contracts, they will be replaced. "
                "Ensure the plugin declares equivalent input_fields/output_artifact_type.",
                plugin_name, cap["name"], cap["name"],
            )
            self._tool_registry.unregister(cap["name"])
            self._tool_registry.register(proxy)
```

- [ ] **Step 2: Run tests**

```bash
uv run pytest tests/plugin/ -x -q
```

Expected: All pass.

- [ ] **Step 3: Commit**

```bash
git add src/plugin/registry.py
git commit -m "fix: add warning when plugin tool overwrites existing host tool (#7)"
```

---

### Task 1.3: Incremental sync from shared ToolRegistry in Agent (#7 Part B)

**Files:**
- Modify: `src/agent/agents/base.py`

- [ ] **Step 1: Store shared registry reference and add sync method**

In `Agent.__init__`, replace lines 134-141 (the tool_registry block):

```python
        # Use provided shared registry as base, or create new one.
        # Plugin tools registered in a shared ToolRegistry after agent
        # construction are incrementally synced on each run() call.
        # See: docs/superpowers/specs/2026-06-08-architecture-fixes-2-design.md #1
        if tool_registry is not None:
            self._shared_tool_registry = tool_registry
            self.tool_registry = ToolRegistry()
            for tool in tool_registry.list_tools():
                self.tool_registry.register(tool)
        else:
            self._shared_tool_registry = None
            self.tool_registry = ToolRegistry()
```

- [ ] **Step 2: Add sync at start of run()**

In `Agent.run()`, add after the `if state is not None:` block (around line 225) and before `system_prompt = self.build_system_prompt(context)`:

```python
        # Incremental sync: discover plugin tools registered after construction.
        # See: docs/superpowers/specs/2026-06-08-plugin-artifact-interaction-fixes-design.md #7
        if self._shared_tool_registry is not None:
            existing = {t.name for t in self.tool_registry.list_tools()}
            for tool in self._shared_tool_registry.list_tools():
                if tool.name not in existing:
                    self.tool_registry.register(tool)
```

- [ ] **Step 3: Run tests**

```bash
uv run pytest tests/agent/test_agent_base.py tests/agent/test_orchestrator.py -x -q
```

Expected: All pass.

- [ ] **Step 4: Commit**

```bash
git add src/agent/agents/base.py
git commit -m "fix: incremental sync plugin tools from shared registry on each Agent.run()"
```

---

## Phase 2: PluginRuntime Streaming + register_tool() (#3)

### Task 2.1: Fix streaming in PluginRuntime._handle_request_async (#3)

**Files:**
- Modify: `src/plugin/sdk/runtime.py`

- [ ] **Step 1: Replace the async generator handling**

In `_handle_request_async`, replace lines 237-246:

```python
            # Determine if handler is an async generator (streaming)
            result = handler(params)
            if inspect.isasyncgen(result):
                # Streaming handler — all yields except the last are streaming
                # chunks; the last yield is the final result.
                chunks: list[Any] = []
                async for chunk in result:
                    chunks.append(chunk)
                for chunk in chunks[:-1]:
                    self._send_chunk(req_id, chunk)
                final_result = chunks[-1] if chunks else None
                self._send_chunk(req_id, None, status="end", result=final_result)
```

With:

```python
            # Determine if handler is an async generator (streaming)
            result = handler(params)
            if inspect.isasyncgen(result):
                # Real streaming: peek one ahead.
                # Each yield (except the last) is sent as a "continue" chunk.
                # The last yield is the final result sent with status="end".
                pending = None
                is_first = True
                async for item in result:
                    if is_first:
                        pending = item
                        is_first = False
                    else:
                        # Send the previous chunk now that we know it's not last
                        self._send_chunk(req_id, pending)
                        pending = item
                # Last item (or only item) = final result
                if pending is not None:
                    self._send_chunk(req_id, None, status="end", result=pending)
                else:
                    # Generator yielded nothing — send empty end
                    self._send_chunk(req_id, None, status="end", result={})
```

- [ ] **Step 2: Run existing tests**

```bash
uv run pytest tests/plugin/ -x -q
```

Expected: All pass (streaming tests still work — same final output, just sent incrementally).

- [ ] **Step 3: Commit**

```bash
git add src/plugin/sdk/runtime.py
git commit -m "fix: implement real streaming in PluginRuntime (send chunks immediately, not buffered)"
```

---

### Task 2.2: Add register_tool() method to PluginRuntime

**Files:**
- Modify: `src/plugin/sdk/runtime.py`

- [ ] **Step 1: Add _pending_caps to __init__**

In `PluginRuntime.__init__`, add after line `self._checker_names: list[str] = checker_names or []`:

```python
        self._pending_caps: list[dict[str, Any]] = []
```

- [ ] **Step 2: Add register_tool() method**

Add before `def on(self, method: str) -> Callable:`:

```python
    def register_tool(self, tool_instance: Any) -> dict[str, Any]:
        """Register a tool instance and auto-extract contract metadata.

        Extracts: name, display_name, description, parameters,
        output_artifact_type, input_fields, output_schema, runtime_policy,
        skip_persist, skip_ref_resolution.
        """
        cap: dict[str, Any] = {
            "type": "tool",
            "name": tool_instance.name,
            "display_name": getattr(tool_instance, "display_name", None),
            "description": tool_instance.description,
            "parameters": tool_instance.parameters,
        }

        # Auto-extract scalar contract fields
        for attr in (
            "output_artifact_type", "output_schema",
            "skip_persist", "skip_ref_resolution",
        ):
            value = getattr(tool_instance, attr, None)
            if value is not None:
                cap[attr] = value

        # input_fields — serialize InputField objects to plain dicts
        input_fields = getattr(tool_instance, "input_fields", None)
        if input_fields:
            cap["input_fields"] = [
                {
                    "name": f.name,
                    "artifact_type": f.artifact_type,
                    "materialize_as": f.materialize_as,
                }
                for f in input_fields
            ]

        # runtime_policy
        rp = getattr(tool_instance, "runtime_policy", None)
        if rp is not None:
            cap["runtime_policy"] = {
                "max_calls": rp.max_calls,
                "max_consecutive": rp.max_consecutive,
            }

        self._pending_caps.append(cap)
        return cap
```

- [ ] **Step 3: Add _collect_capabilities() and update run()**

Add method:
```python
    def _collect_capabilities(self) -> tuple[list[dict[str, Any]], str]:
        """Collect caps from register_capabilities() AND register_tool() calls.

        Returns (capabilities_list, system_prompt).
        """
        caps_result = self.register_capabilities()
        system_prompt = ""
        if isinstance(caps_result, dict):
            caps = caps_result.get("capabilities", [])
            system_prompt = caps_result.get("system_prompt", "")
        elif isinstance(caps_result, list):
            caps = caps_result
        else:
            caps = []

        if not isinstance(caps, list):
            caps = []

        caps.extend(self._pending_caps)
        return caps, system_prompt
```

In `run()`, replace lines 121-133 (the caps_result extraction block):

```python
        # Collect tool/checker names for built-in list handlers
        caps, system_prompt = self._collect_capabilities()

        for cap in caps:
```

(Keep the rest of the loop: `if cap.get("type") == "tool" ...`)

Also replace the `_send_notification` call's system_prompt to use the extracted one:
```python
        self._send_notification(METHOD_REGISTER, {
            "capabilities": caps,
            "system_prompt": system_prompt,
        })
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/plugin/ -x -q
```

Expected: All pass.

- [ ] **Step 5: Commit**

```bash
git add src/plugin/sdk/runtime.py
git commit -m "feat: add register_tool() to PluginRuntime for auto-extracting contract metadata"
```

---

### Task 2.3: Migrate plugin entry.py files to use register_tool()

**Files:**
- Modify: `plugins/parse/entry.py`
- Modify: `plugins/content_audit/entry.py`
- Modify: `plugins/format_audit/entry.py`
- Modify: `plugins/style_audit/entry.py`
- Modify: `plugins/plagiarism/entry.py`
- Modify: `plugins/text_correction/entry.py`
- Modify: `plugins/search/entry.py`
- Modify: `plugins/annotate/entry.py`
- Modify: `plugins/template/entry.py`

- [ ] **Step 1: Migrate parse/entry.py**

```python
"""Parse plugin — document file parsing."""
from src.plugin.sdk import PluginRuntime
from tools import ParseTool


class ParsePlugin(PluginRuntime):
    def register_capabilities(self):
        return {"system_prompt": (
            "# 文档解析\n\n"
            "## 能力\n将 PDF、DOCX、扫描图片解析为结构化的 Document 模型。\n"
            "## 使用方式\n调用 `parse_document` 工具，传入 `file_path` 参数（绝对路径）。\n"
        )}

    def _setup_handlers(self):
        tool = ParseTool()
        self.register_tool(tool)

        @self.on("tool.execute")
        async def handle_tool_execute(params):
            tool_name = params["tool"]
            args = params.get("args", {})
            if tool_name == "parse_document":
                result = await tool.execute(**args)
                return {"success": result.success, "data": result.data, "error": result.error, "metadata": result.metadata}
            return {"success": False, "error": f"Unknown tool: {tool_name}"}


if __name__ == "__main__":
    import asyncio
    asyncio.run(ParsePlugin().run())
```

- [ ] **Step 2: Migrate search/entry.py** — same pattern: create tool, `self.register_tool(tool)`, keep handler.

- [ ] **Step 3: Migrate content_audit/entry.py** — same pattern.

- [ ] **Step 4: Migrate format_audit/entry.py** — same pattern. Multiple tools: call `register_tool()` for each.

- [ ] **Step 5: Migrate style_audit/entry.py** — same pattern.

- [ ] **Step 6: Migrate plagiarism/entry.py** — same pattern. Multiple tools.

- [ ] **Step 7: Migrate text_correction/entry.py** — same pattern. Multiple tools.

- [ ] **Step 8: Migrate annotate/entry.py** — same pattern.

- [ ] **Step 9: Migrate template/entry.py** — same pattern.

- [ ] **Step 10: Run plugin integration tests**

```bash
uv run pytest tests/plugin/ tests/test_app_plugin_integration.py -x -q
```

Expected: All pass.

- [ ] **Step 11: Commit**

```bash
git add plugins/*/entry.py
git commit -m "refactor: migrate all plugin entry.py to use register_tool()"
```

---

## Phase 3: JSONRPCClient Stream Race Fix (#5)

### Task 3.1: Add _closed checks to stream() loop

**Files:**
- Modify: `src/plugin/client.py`

- [ ] **Step 1: Add _closed checks in stream()**

In `JSONRPCClient.stream()`, add two `_closed` checks. First, at the top of the while loop (after line 240 `while True:`):

```python
        while True:
            # Check closed state at top of each iteration (#5)
            if self._closed:
                raise PluginCrashedError(self.plugin_name)
```

Second, after creating the future and adding to _pending (after line 257 `self._pending[req_id] = future`):

```python
            future: asyncio.Future = asyncio.get_event_loop().create_future()
            self._pending[req_id] = future

            # Check closed again after adding to _pending (#5)
            # close() might have run between iterations — the future
            # was just created so _fail_all_pending() missed it.
            if self._closed:
                self._pending.pop(req_id, None)
                raise PluginCrashedError(self.plugin_name)
```

- [ ] **Step 2: Run tests**

```bash
uv run pytest tests/plugin/ -x -q
```

Expected: All pass.

- [ ] **Step 3: Commit**

```bash
git add src/plugin/client.py
git commit -m "fix: add _closed checks in JSONRPCClient.stream() to prevent hang on crash (#5)"
```

---

## Phase 4: Final Verification

### Task 4.1: Write integration test for ProxyAgent host-object filtering

**Files:**
- Create: `tests/plugin/test_proxy_agent_dispatch.py`

- [ ] **Step 1: Write the test**

```python
"""Test that ProxyAgent correctly filters host-side objects from JSON-RPC params."""
import pytest
from src.plugin.proxies import ProxyAgent
from src.plugin.protocol import JSONRPCStreamChunk


class RecordingClient:
    """Records the params sent to stream() without actually serializing."""
    def __init__(self):
        self.last_method = None
        self.last_params = None

    async def stream(self, method, params=None, heartbeat_timeout=60.0):
        self.last_method = method
        self.last_params = params
        yield JSONRPCStreamChunk(
            id=1, status="end",
            result={"status": "completed", "content": "ok"},
        )

    @property
    def plugin_name(self):
        return "test"


class TestProxyAgentHostObjectFiltering:
    """Verify #1: ProxyAgent._HOST_KWARGS filters all host-side objects."""

    def test_filters_artifact_store(self):
        agent_spec = {"name": "test_agent", "role": "tester"}
        client = RecordingClient()
        proxy = ProxyAgent(client, agent_spec)

        # Simulate SubAgentRunner.dispatch() call with host-side objects
        import asyncio
        asyncio.run(proxy.run(
            task="audit doc",
            context={"file_path": "/tmp/test.pdf"},
            context_manager=object(),      # host object — must be filtered
            artifact_store=object(),       # host object — must be filtered
            audit_logger=object(),         # host object — must be filtered
            on_step=lambda e, d: None,     # callable — must be filtered
            on_token=lambda t: None,       # callable — must be filtered
            on_content_token=lambda t: None,  # callable — must be filtered
            on_tool_result=lambda n, r, s: None,  # callable — must be filtered
        ))

        params = client.last_params
        assert params is not None, "stream() should have been called"
        assert "task" in params
        assert params["task"] == "audit doc"

        # Verify host objects are NOT forwarded
        for key in (
            "context", "context_manager", "artifact_store",
            "audit_logger", "on_step", "on_token",
            "on_content_token", "on_tool_result", "input", "state",
        ):
            assert key not in params, f"Host object '{key}' should be filtered"

    def test_passes_serializable_kwargs(self):
        agent_spec = {"name": "test_agent", "role": "tester"}
        client = RecordingClient()
        proxy = ProxyAgent(client, agent_spec)

        import asyncio
        asyncio.run(proxy.run(
            task="audit doc",
            extra_info="this should pass through",  # serializable — should be kept
        ))

        params = client.last_params
        assert params["extra_info"] == "this should pass through"
```

- [ ] **Step 2: Run the test and verify it passes**

```bash
uv run pytest tests/plugin/test_proxy_agent_dispatch.py -x -v
```

Expected: 2 passed.

- [ ] **Step 3: Commit**

```bash
git add tests/plugin/test_proxy_agent_dispatch.py
git commit -m "test: add ProxyAgent host-object filtering test (#1)"
```

---

### Task 4.2: Write integration test for ProxyTool contract propagation

**Files:**
- Create: `tests/plugin/test_proxy_tool_contract.py`

- [ ] **Step 1: Write the test**

```python
"""Test that ProxyTool correctly reads contract fields from plugin tool spec."""
import pytest
from src.plugin.proxies import ProxyTool


class MockClient:
    async def call(self, method, params=None, timeout=30.0):
        return {"success": True, "data": {}}

    @property
    def plugin_name(self):
        return "test"


class TestProxyToolContractPropagation:
    """Verify #2: ProxyTool reads contract fields from tool_spec."""

    def test_reads_output_artifact_type(self):
        tool_spec = {
            "name": "parse_document",
            "description": "Parse a document",
            "output_artifact_type": "docaudit.parsed_document",
        }
        proxy = ProxyTool(MockClient(), tool_spec)
        assert proxy.output_artifact_type == "docaudit.parsed_document"
        # Legacy compat
        assert proxy.output_contract is None

    def test_reads_input_fields(self):
        from src.agent.artifacts.models import InputField
        tool_spec = {
            "name": "audit_content",
            "description": "Audit content",
            "input_fields": [
                {"name": "paragraphs", "artifact_type": "docaudit.paragraph_list"},
            ],
        }
        proxy = ProxyTool(MockClient(), tool_spec)
        assert len(proxy.input_fields) == 1
        f = proxy.input_fields[0]
        assert isinstance(f, InputField)
        assert f.name == "paragraphs"
        assert f.artifact_type == "docaudit.paragraph_list"

    def test_reads_runtime_policy(self):
        from src.agent.artifacts.models import RuntimePolicy
        tool_spec = {
            "name": "limited_tool",
            "description": "Has limits",
            "runtime_policy": {"max_calls": 3, "max_consecutive": 1},
        }
        proxy = ProxyTool(MockClient(), tool_spec)
        assert isinstance(proxy.runtime_policy, RuntimePolicy)
        assert proxy.runtime_policy.max_calls == 3
        assert proxy.runtime_policy.max_consecutive == 1

    def test_reads_skip_persist(self):
        tool_spec = {
            "name": "ephemeral_tool",
            "description": "Don't cache me",
            "skip_persist": True,
        }
        proxy = ProxyTool(MockClient(), tool_spec)
        assert proxy.skip_persist is True

    def test_defaults_when_no_contract(self):
        tool_spec = {"name": "simple_tool", "description": "No contracts"}
        proxy = ProxyTool(MockClient(), tool_spec)
        assert proxy.output_artifact_type is None
        assert proxy.input_fields == ()
        assert proxy.runtime_policy is None
        assert proxy.skip_persist is False
```

- [ ] **Step 2: Run and verify**

```bash
uv run pytest tests/plugin/test_proxy_tool_contract.py -x -v
```

Expected: 5 passed.

- [ ] **Step 3: Commit**

```bash
git add tests/plugin/test_proxy_tool_contract.py
git commit -m "test: add ProxyTool contract propagation test (#2)"
```

---

### Task 4.3: Run full test suite

- [ ] **Step 1: Run all tests**

```bash
uv run pytest tests/ -x -q
```

Expected: All pass. Fix any failures before proceeding.

- [ ] **Step 2: Commit if any final fixes were needed**

```bash
git add -A
git commit -m "chore: final test suite fixes after plugin-artifact interaction changes"
```

---

## Summary

| Phase | Tasks | Files Changed | Issues |
|-------|-------|---------------|--------|
| 0 | 0.1–0.5 | `models.py`, `resolver.py`, `registry.py`, `loop_hints.py`, 8 tool files | Foundation |
| 1 | 1.1–1.3 | `proxies.py`, `registry.py`, `base.py` | #1, #2, #6, #7, #8 |
| 2 | 2.1–2.3 | `runtime.py`, 9 `entry.py` files | #3 |
| 3 | 3.1 | `client.py` | #5 |
| 4 | 4.1–4.3 | 2 new test files | Verification |

Total: ~14 tasks, ~22 files, ~450 lines changed.
