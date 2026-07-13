# Plugin-Artifact-Agent Loop Interaction Fixes — Design

**Date**: 2026-06-08  
**Status**: Approved  
**Scope**: 8 newly discovered issues in plugin system × agent loop × artifact system interaction, plus contract model simplification as foundation.

---

## Issues Summary

| # | Severity | Issue | File |
|---|----------|-------|------|
| 1 | CRITICAL | ProxyAgent._HOST_KWARGS incomplete — JSON serialization crash | `proxies.py` |
| 2 | CRITICAL | Plugin tools invisible to artifact contract system | `proxies.py`, `registry.py` |
| 3 | HIGH | PluginRuntime buffers all streaming chunks — no real streaming | `sdk/runtime.py` |
| 4 | HIGH | Plugin agents isolated from host infrastructure | `proxies.py` (design choice) |
| 5 | HIGH | JSON-RPC stream close race condition | `client.py` |
| 6 | MEDIUM | ProxyTool always persists (skip_persist hardcoded) | `proxies.py` |
| 7 | MEDIUM | Host/plugin tool name collision (e.g. parse_document) | `registry.py`, `base.py` |
| 8 | MEDIUM | Plugin agent bypasses artifact scoping | `proxies.py` |

**Relation to existing Round 2 design doc** (`docs/superpowers/specs/2026-06-08-architecture-fixes-2-design.md`):
- Round 2 Issue #1 (Agent snapshots ToolRegistry) is partly addressed here via the incremental-sync fix that is also needed for #7.
- Round 2 Issue #2 (duplicate handling) is related to #7 here.
- All 8 issues here are NEW — not covered by Round 2.

---

## Part 0: Contract Model Simplification (Foundation)

### 0.1 Rationale

Current contract models (`ToolInputContract`, `ToolOutputContract`, `OutputArtifactContract`, `ContractField`, `ToolRuntimePolicy`) contain 20+ fields across 5 Pydantic models. Analysis of all existing tool implementations shows that only `artifact_type` and `name` ever differ from defaults:

- `role` is always `"primary_document"` (input) or `"intermediate"` (output)
- `subject` is always `"current_upload"` (input) or `"unknown"` (output)
- `schema_version` is always `"1.0"`
- `persist` is always `"auto"`
- `llm_visible` is always `"summary"`
- `projection_allowed` is always `True`
- `debug_only` is always `False`
- `required` is always `True`
- `allow_explicit_override` is always `True`
- `constraints` is always empty

The `role`/`subject` matching in `resolver._artifact_allowed()` has never rejected an artifact in practice because all artifacts share the same role/subject values within a single agent run.

**Decision**: Remove `role`/`subject` matching. Match artifacts purely by `artifact_type`. This eliminates 3 Pydantic models and simplifies both the host-side resolver and plugin-side tool declaration.

### 0.2 New Simplified Models

```python
# src/agent/artifacts/models.py — new additions

@dataclass(frozen=True)
class InputField:
    """A single input field requirement for a tool.
    
    Declares that the tool's parameter ``name`` should be auto-bound
    from an artifact of ``artifact_type``.
    """
    name: str
    artifact_type: str
    materialize_as: str | None = None  # derived from artifact_type if None


@dataclass(frozen=True)
class RuntimePolicy:
    """Runtime safety policy for a tool."""
    max_calls: int | None = None
    max_consecutive: int | None = None
```

Tools declare contracts directly as class-level attributes:

```python
class ParseTool:
    name = "parse_document"
    output_artifact_type: str | None = "docaudit.parsed_document"
    input_fields: tuple[InputField, ...] = ()
    runtime_policy: RuntimePolicy | None = None
    skip_persist: bool = False
    skip_ref_resolution: bool = False
    output_schema: dict | None = None
```

### 0.3 Resolver Simplification

`ProjectionResolver._artifact_allowed()` currently matches on `artifact_type` AND `role` AND `subject` AND `scope` AND `sensitivity`:

```python
# Before (resolver.py:238-264)
@staticmethod
def _artifact_allowed(artifact, field, policy):
    if artifact.metadata.debug_only and not policy.allow_debug_artifacts:
        return False
    if not artifact.metadata.projection_allowed and not policy.allow_debug_artifacts:
        return False
    if artifact.metadata.semantic_role != field.role:        # ← REMOVE
        return False
    if artifact.metadata.subject != field.subject:           # ← REMOVE
        return False
    # scope constraint check                                   ← REMOVE
    # sensitivity gate                                          ← REMOVE
    return True

# After
@staticmethod
def _artifact_allowed(artifact, field, policy):
    if artifact.metadata.debug_only and not policy.allow_debug_artifacts:
        return False
    if not artifact.metadata.projection_allowed:
        return False
    return True
```

`ContractBinder.bind_tool_inputs()` is updated to accept `list[InputField]` instead of `ToolInputContract`. The `ToolInputContract` class is kept (deprecated but not removed) for backward compatibility during the transition — `bind_tool_inputs` accepts both forms.

### 0.4 Impact on Existing Tools

All existing tools that define contracts are updated:

| Tool | Old | New |
|------|-----|-----|
| `ParseTool` | `output_contract = ToolOutputContract(...)` | `output_artifact_type = "docaudit.parsed_document"` |
| `AuditContentTool` | `input_contract = ToolInputContract(fields=(ContractField(...),))` | `input_fields = (InputField("paragraphs", "docaudit.paragraph_list"),)` |
| `AuditFormatTool` | `input_contract = ToolInputContract(...)` | `input_fields = (InputField("document", "docaudit.parsed_document"),)` |
| `SearchDocumentsTool` | `output_contract = ToolOutputContract(...)` | `output_artifact_type = "docaudit.search_results"` |
| tools with `runtime_policy` | `ToolRuntimePolicy(max_calls_per_agent_run=N)` | `RuntimePolicy(max_calls=N)` |

---

## Part 1: ProxyTool/ProxyAgent Fixes (Issues #1, #2, #6, #7, #8)

### 1.1 Issue #1 — Complete ProxyAgent._HOST_KWARGS

**Root cause**: `ProxyAgent._HOST_KWARGS` only filters `input`, `context`, `state`. When `SubAgentRunner.dispatch()` calls `agent.run()`, it passes 8 additional host-side objects that are not JSON-serializable.

**Fix**: Add all host-side objects to `_HOST_KWARGS`:

```python
# src/plugin/proxies.py — ProxyAgent
_HOST_KWARGS = frozenset({
    "input", "context", "state",
    "artifact_store", "context_manager", "audit_logger",
    "on_step", "on_token", "on_content_token", "on_tool_result",
})
```

Note: `task` is intentionally NOT filtered — it is the primary data channel to the plugin agent and is always a JSON-serializable string.

After this fix, plugin agents receive only `task` and `**kwargs` that exclude host objects. Data that the plugin agent needs (document content, refs) must be passed in the `task` string or explicitly included by the caller. This is correct for the current architecture where plugin agents run isolated agent loops.

### 1.2 Issue #2 — Propagate Contract Fields to ProxyTool

**Root cause**: `ProxyTool.__init__()` hardcodes `input_contract=None`, `output_contract=None`, `output_schema=None`, `runtime_policy=None`, `skip_persist=False`, `skip_ref_resolution=False`. These fields are never read from the plugin tool specification.

**Fix**: Read contract fields from `tool_spec` dict passed to `ProxyTool.__init__()`, matching the simplified model from Part 0:

```python
# src/plugin/proxies.py — ProxyTool
class ProxyTool:
    def __init__(self, client, tool_spec):
        self._client = client
        self.name = tool_spec["name"]
        self.display_name = tool_spec.get("display_name")
        self.description = tool_spec.get("description", "")
        self.parameters = tool_spec.get("parameters", {})

        # Contract fields — read from plugin spec if present
        self.output_artifact_type: str | None = tool_spec.get("output_artifact_type")
        self.input_fields: tuple = tuple(
            InputField(**f) for f in tool_spec.get("input_fields", [])
        ) if "input_fields" in tool_spec else ()
        self.output_schema: dict | None = tool_spec.get("output_schema")
        self.skip_persist: bool = tool_spec.get("skip_persist", False)
        self.skip_ref_resolution: bool = tool_spec.get("skip_ref_resolution", False)
        
        rp = tool_spec.get("runtime_policy")
        self.runtime_policy: RuntimePolicy | None = (
            RuntimePolicy(**rp) if rp else None
        )
        
        # Legacy compat aliases (for code that checks these attrs)
        self.input_contract: Any | None = None  # deprecated, kept for compat
        self.output_contract: Any | None = None  # deprecated, kept for compat
```

**Changes in `ToolRegistry.execute()`**: Update the check at line 131 to also use `output_artifact_type` and `input_fields`:

```python
# src/agent/tools/registry.py
# Auto-bind: check both old and new field names
input_fields = getattr(tool, "input_fields", None)
input_contract = getattr(tool, "input_contract", None)

if (artifact_store is not None 
    and input_fields is not None 
    and self._policy.features.tool_auto_binding_enabled):
    # Use new simplified path
    ...
elif (artifact_store is not None 
      and input_contract is not None 
      and self._policy.features.tool_auto_binding_enabled):
    # Legacy path — use ContractField-based binding
    ...

# Output registration: check both
output_artifact_type = getattr(tool, "output_artifact_type", None)
output_contract = getattr(tool, "output_contract", None)
if artifact_store is not None and output_artifact_type is not None and result.success:
    self._register_output_artifact_simple(
        tool_name=name, artifact_type=output_artifact_type,
        result=result, artifact_store=artifact_store,
    )
elif artifact_store is not None and output_contract is not None and result.success:
    self._register_output_artifact(...)  # Legacy path
```

### 1.3 Issue #6 — skip_persist from Plugin Spec

Resolved by 1.2 above: `skip_persist` is now read from `tool_spec`. Plugin tools that don't want their output persisted set `skip_persist: true` in their capability declaration.

### 1.4 Issue #7 — Host/Plugin Tool Name Collision

**Root cause**: Plugin tools (e.g., parse plugin's `parse_document`) can silently overwrite host-side tools with the same name. The overwrite is intentional (plugin takes priority) but the host-side contract fields are lost in the process.

**Fix**: Two-part fix:

**Part A** — Warning on collision in `ExtensionRegistry._register_tool()`:

```python
# src/plugin/registry.py
def _register_tool(self, plugin_name, client, cap):
    if self._tool_registry is None:
        return
    proxy = ProxyTool(client, cap)
    try:
        self._tool_registry.register(proxy)
    except ValueError:
        logger.warning(
            "Plugin '%s' tool '%s' overwrites existing tool. "
            "If the existing tool had artifact contracts, they will be replaced. "
            "Ensure the plugin tool declares equivalent input_fields/output_artifact_type.",
            plugin_name, cap["name"],
        )
        self._tool_registry.unregister(cap["name"])
        self._tool_registry.register(proxy)
```

**Part B** — Incremental sync (also addresses Round 2 #1). In `Agent.__init__`, retain reference to shared registry and sync on each `run()`:

```python
# src/agent/agents/base.py
# In __init__:
if tool_registry is not None:
    self._shared_tool_registry = tool_registry
    self.tool_registry = ToolRegistry()
    for tool in tool_registry.list_tools():
        self.tool_registry.register(tool)
else:
    self._shared_tool_registry = None
    self.tool_registry = ToolRegistry()

# In run(), before calling agent_loop:
async def run(self, ...):
    ...
    if self._shared_tool_registry is not None:
        existing = {t.name for t in self.tool_registry.list_tools()}
        for tool in self._shared_tool_registry.list_tools():
            if tool.name not in existing:
                self.tool_registry.register(tool)
    
    final_state = await agent_loop(...)
```

### 1.5 Issue #8 — Plugin Agent Artifact Scoping

This issue is resolved by Issue #1 — once `artifact_store` is correctly filtered from kwargs, the `ScopedArtifactStore` is no longer sent via JSON-RPC. Plugin agents receive no artifact store reference (which is correct — they run in isolated subprocesses). Any artifact data the plugin agent needs must be passed in the `task` string.

### 1.6 ToolRegistry Dual-Path Adaptations

`ToolRegistry.execute()` needs to handle both legacy contracts and the new simplified fields. Three changes:

**1. Auto-bind input fields:** Check `input_fields` (new) before `input_contract` (legacy):

```python
# src/agent/tools/registry.py — in execute()
input_fields = getattr(tool, "input_fields", None)
input_contract = getattr(tool, "input_contract", None)

if artifact_store is not None and input_fields is not None and input_fields:
    # New simplified path — build a synthetic ToolInputContract from input_fields
    synthetic_contract = ToolInputContract(
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
    binding_result = self._bind_contract_arguments(
        input_contract=synthetic_contract,
        artifact_store=artifact_store,
        explicit_kwargs=kwargs,
    )
    ...
elif artifact_store is not None and input_contract is not None:
    # Legacy path
    ...
```

The synthetic contract is a bridge — it converts the new simple `InputField` list into the legacy `ToolInputContract` format that `ContractBinder` expects. Once all tools migrate to the new format, `ContractBinder` can be updated to accept `InputField` directly.

**2. Output artifact registration:** Add `_register_output_artifact_simple`:

```python
# src/agent/tools/registry.py
def _register_output_artifact_simple(
    self, *, tool_name: str, artifact_type: str,
    result: ToolResult, artifact_store: ArtifactStore,
) -> None:
    """Register tool output as a typed artifact using simplified contract."""
    if not result.success or result.data is None:
        return
    data = result.data
    ref_id = f"$ref:{tool_name}:latest"

    # If data is a persisted-output marker, resolve it
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

**3. Runtime policy enforcement:** Check `runtime_policy` attribute (both new `RuntimePolicy` and legacy `ToolRuntimePolicy`):

```python
# In _check_runtime_policy, accept both:
runtime_policy = getattr(tool, "runtime_policy", None)
if isinstance(runtime_policy, RuntimePolicy):
    refused = self._check_runtime_policy_simple(name, runtime_policy)
elif isinstance(runtime_policy, ToolRuntimePolicy):
    refused = self._check_runtime_policy(name, runtime_policy, kwargs)
```

### 1.7 loop_hints.py Update

The three functions in `loop_hints.py` (`_is_terminal_tool_ready`, `_get_ready_terminal_tools`, `_build_blocked_tools_hints`) currently check `input_contract` and `require_contract_binding`. Update them to also check `input_fields`:

```python
# In each function, change:
input_contract = getattr(tool, "input_contract", None)
if input_contract is None or not input_contract.require_contract_binding:
    continue

# To:
input_fields = getattr(tool, "input_fields", None)
input_contract = getattr(tool, "input_contract", None)
if input_fields is not None and input_fields:
    # Build synthetic contract for resolver (same pattern as ToolRegistry)
    synthetic = ToolInputContract(
        tool_name=tool.name,
        fields=tuple(
            ContractField(
                name=f.name, artifact_type=f.artifact_type,
                role="primary_document", subject="current_upload",
                materialize_as=f.materialize_as or "pass_through",
            )
            for f in input_fields
        ),
    )
    # Use synthetic for resolution
elif input_contract is not None:
    # Legacy path
    synthetic = input_contract
else:
    continue
```

The rest of the hint logic (resolver.resolve, build hints text) works the same — only the contract source changes.

This issue is resolved by Issue #1 — once `artifact_store` is correctly filtered from kwargs, the `ScopedArtifactStore` is no longer sent via JSON-RPC. Plugin agents receive no artifact store reference (which is correct — they run in isolated subprocesses). Any artifact data the plugin agent needs must be passed in the `task` string.

---

## Part 2: PluginRuntime Streaming + register_tool() (Issue #3)

### 2.1 Issue #3 — Real Streaming

**Root cause**: `PluginRuntime._handle_request_async()` collects all chunks from the async generator into a list before sending any. This defeats the purpose of the streaming protocol and causes heartbeat timeouts on the host side for long-running agent tasks.

**Fix**: Use peek-one-ahead. The async generator protocol uses position to distinguish streaming chunks from the final result: all except the last yield are streaming chunks (status="continue"); the last yield is the final result (status="end"). We send the PREVIOUS chunk when we get the NEXT one. For the last yield, send as end:

```python
if inspect.isasyncgen(result):
    pending = None
    is_first = True
    async for item in result:
        if is_first:
            pending = item
            is_first = False
        else:
            # Send the previous chunk as "continue"
            self._send_chunk(req_id, pending)
            pending = item
    # Last item = final result
    if pending is not None:
        self._send_chunk(req_id, None, status="end", result=pending)
    else:
        # Generator yielded nothing — send empty end
        self._send_chunk(req_id, None, status="end", result={})
```

This approach:
- Sends each chunk immediately (after receiving the next one, confirming it's not the last)
- Last chunk is sent as the "end" result
- Works correctly for generators that yield 0, 1, or N items

### 2.2 New `register_tool()` Method

Add a convenience method to PluginRuntime that extracts contract metadata from a tool instance. Handler registration remains explicit (plugin author writes `@self.on("tool.execute")` themselves):

```python
# src/plugin/sdk/runtime.py
class PluginRuntime:
    def __init__(self, ...):
        self._pending_caps: list[dict] = []
    
    def register_tool(self, tool_instance):
        """Extract contract metadata from tool instance for register notification.
        
        Auto-extracts: name, display_name, description, parameters,
        output_artifact_type, input_fields, output_schema, runtime_policy,
        skip_persist, skip_ref_resolution.
        """
        cap = {
            "type": "tool",
            "name": tool_instance.name,
            "display_name": getattr(tool_instance, "display_name", None),
            "description": tool_instance.description,
            "parameters": tool_instance.parameters,
        }
        
        # Auto-extract contract fields
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
                {"name": f.name, "artifact_type": f.artifact_type,
                 "materialize_as": f.materialize_as}
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
    
    def _collect_capabilities(self) -> list[dict]:
        """Collect caps from register_capabilities() AND register_tool()."""
        caps_result = self.register_capabilities()
        if isinstance(caps_result, dict):
            caps = caps_result.get("capabilities", [])
        elif isinstance(caps_result, list):
            caps = caps_result
        else:
            caps = []
        caps.extend(self._pending_caps)
        return caps
```

`_collect_capabilities()` is called in `run()` instead of directly calling `register_capabilities()`.

### 2.3 Plugin Migration Example

Before (parse plugin):
```python
class ParsePlugin(PluginRuntime):
    def register_capabilities(self):
        return {
            "capabilities": [
                {"type": "tool", "name": "parse_document", "display_name": "解析文档",
                 "description": ParseTool.description, "parameters": ParseTool.parameters},
            ],
            "system_prompt": "...",
        }

    def _setup_handlers(self):
        tool = ParseTool()
        @self.on("tool.execute")
        async def handle_tool_execute(params):
            ...
```

After:
```python
class ParsePlugin(PluginRuntime):
    def register_capabilities(self):
        return {"system_prompt": "..."}

    def _setup_handlers(self):
        tool = ParseTool()
        self.register_tool(tool)  # ← one line, auto-extracts all metadata

        @self.on("tool.execute")
        async def handle_tool_execute(params):
            ...
```

---

## Part 3: JSONRPCClient Stream Race Fix (Issue #5)

### 3.1 Root Cause

In `JSONRPCClient.stream()`, the loop creates a new future for each chunk. If the plugin crashes between two chunk-processing iterations (after one future resolves but before the next is created and added to `_pending`), `close()` runs `_fail_all_pending()` on the empty `_pending` dict. The newly created future is then added to `_pending` but nothing resolves it — hangs until `heartbeat_timeout` (60s).

### 3.2 Fix

Add `_closed` check at the start of each iteration, and immediately after creating a new future:

```python
# src/plugin/client.py
async def stream(self, method, params=None, heartbeat_timeout=60.0):
    if self._closed:
        raise PluginCrashedError(self.plugin_name)
    
    await self._ensure_reader()
    
    self._next_id += 1
    req_id = self._next_id
    
    request = JSONRPCRequest(id=req_id, method=method, params=params or {})
    self._writer.write((request.model_dump_json() + "\n").encode("utf-8"))
    await self._writer.drain()
    
    while True:
        # Check closed state at top of each iteration
        if self._closed:
            raise PluginCrashedError(self.plugin_name)
        
        # Check buffer first
        if req_id in self._response_buffer:
            buf = self._response_buffer[req_id]
            raw = buf.pop(0)
            if "error" in raw and raw["error"] is not None:
                err = raw["error"]
                if not buf:
                    del self._response_buffer[req_id]
                raise PluginRPCError(err.get("code", -1), err.get("message", "Unknown error"))
            if not buf:
                del self._response_buffer[req_id]
        else:
            future = asyncio.get_event_loop().create_future()
            self._pending[req_id] = future
            
            # Check closed again after adding to _pending
            # (close() might have run between future creation and adding to _pending)
            if self._closed:
                self._pending.pop(req_id, None)
                raise PluginCrashedError(self.plugin_name)
            
            try:
                raw = await asyncio.wait_for(future, timeout=heartbeat_timeout)
            except asyncio.TimeoutError:
                self._pending.pop(req_id, None)
                raise
        
        if not isinstance(raw, dict):
            yield JSONRPCStreamChunk(id=req_id, chunk=str(raw), status="end", result=raw)
            break
        
        chunk = JSONRPCStreamChunk.model_validate(raw)
        yield chunk
        if chunk.is_end:
            break
```

Key addition: two `if self._closed` checks — one at the top of each iteration and one immediately after creating the future. If `close()` ran between iterations, the future is popped from `_pending` and a `PluginCrashedError` is raised immediately.

---

## Implementation Order

| Phase | Files | Issues | ~Lines |
|-------|-------|--------|--------|
| 0 — Contract simplify | `models.py`, `resolver.py`, `binder.py`, tool classes | Foundation | ~120 |
| 1 — Proxy fixes | `proxies.py`, `registry.py`, `base.py` | #1, #2, #6, #7, #8 | ~50 |
| 2 — ToolRegistry adapt | `registry.py` (tools) | Adapt to simplified contracts | ~30 |
| 3 — Runtime fix | `sdk/runtime.py` | #3 + `register_tool()` | ~40 |
| 4 — Plugin migration | 8 × `entry.py` | Use `register_tool()` | ~80 |
| 5 — Client fix | `client.py` | #5 | ~10 |
| 6 — Tests | `test_proxies.py`, `test_registry.py`, plugin tests | Verify all fixes | ~100 |

Total: ~430 lines changed across ~20 files.

---

## Risks

1. **Contract simplification** — Removing `role`/`subject` matching could theoretically cause incorrect artifact binding if multiple artifacts of the same type but different roles exist. In practice, single-run agent sessions only have one document, so this is safe. The matching can be re-added later if multi-document workflows require it.

2. **Plugin agent data passing** — After fixing #1, plugin agents receive only `task` string. Plugin implementations that try to access `artifact_store` or `context_manager` will break. Current plugins don't do this, so impact is zero.

3. **Streaming change** — The peek-one-ahead approach adds one-chunk latency (each chunk is sent when the NEXT chunk arrives). This is acceptable for `agent.run` where chunks are reasoning/response text. If sub-chunk latency matters, a more sophisticated approach (like a timeout-based flush) can be added later.
