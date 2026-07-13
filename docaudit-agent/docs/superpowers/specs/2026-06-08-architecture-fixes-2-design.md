# Architecture Fixes (Round 2) — Design

**Date**: 2026-06-08  
**Status**: Approved  
**Scope**: 11 architecture issues identified in code review, covering plugin system, agent framework, and cross-module interactions

---

## Issues and Fixes

### Issue 1 (HIGH): Agent snapshots ToolRegistry at construction time

**Root cause**: `Agent.__init__` (`src/agent/agents/base.py:134-137`) copies tools from the shared `ToolRegistry` once at construction time. Plugin tools are registered during `PluginSystem.start()` (FastAPI lifespan), which happens AFTER agent construction. Plugin-provided tools are therefore invisible at runtime.

**Fix**: Retain a reference to the shared registry, incremental-sync on each `run()` call:

```python
# base.py Agent.__init__
if tool_registry is not None:
    self._shared_tool_registry = tool_registry
    self.tool_registry = ToolRegistry()
    for tool in tool_registry.list_tools():
        self.tool_registry.register(tool)
else:
    self._shared_tool_registry = None
    self.tool_registry = ToolRegistry()
```

```python
# base.py Agent.run() — before agent_loop call:
if self._shared_tool_registry is not None:
    existing = {t.name for t in self.tool_registry.list_tools()}
    for tool in self._shared_tool_registry.list_tools():
        if tool.name not in existing:
            self.tool_registry.register(tool)
```

**Rationale**: Minimal change (~10 lines), preserves per-agent isolation of `ToolRegistry` (run-state counters), no changes to `agent_loop` or `ProcessManager`.

---

### Issue 2 (HIGH): Inconsistent duplicate-handling across registries

**Root cause**: `ToolRegistry.register()` raises `ValueError` on duplicate. `ExtensionRegistry._register_tool()` catches it and silently overwrites. Different callers experience different behavior.

**Fix**: Add `force` parameter to `ToolRegistry.register()`:

```python
# registry.py
def register(self, tool: ToolProtocol, force: bool = False) -> None:
    if tool.name in self._tools:
        if force:
            del self._tools[tool.name]
        else:
            raise ValueError(f"Duplicate tool name: {tool.name}")
    self._tools[tool.name] = tool
```

`ExtensionRegistry._register_tool()` calls `register(proxy, force=True)`.

**Rationale**: Explicit is better than implicit. Callers that want overwrite semantics must opt in. The default (non-force) remains safe.

---

### Issue 3 (HIGH): No circuit breaker for immediate re-crashes

**Root cause**: `_on_crash` (`manager.py:286-307`) uses exponential backoff for all restart attempts, even when the plugin crashes within seconds of startup (indicating a deterministic bug, not a transient failure).

**Fix**: Track plugin uptime, skip restart if it crashes within the startup window:

```python
IMMEDIATE_CRASH_WINDOW = 5.0  # seconds

# In _start_one, after marking ACTIVE:
proc._started_at = asyncio.get_event_loop().time()

# In _on_crash, before retry logic:
if proc._started_at > 0:
    uptime = asyncio.get_event_loop().time() - proc._started_at
    if uptime < IMMEDIATE_CRASH_WINDOW:
        proc.state = PluginState.FATAL
        logger.error("Plugin '%s' crashed within %.1fs of startup, marking FATAL", ...)
        return
```

Add `_started_at: float = 0.0` field to `PluginProcess` dataclass.

---

### Issue 4 (MEDIUM): Monolithic agent_loop (~450 lines)

**Root cause**: `agent_loop` in `src/agent/core/loop.py` handles ~15 distinct concerns in one function.

**Fix**: Extract two new phase functions in `src/agent/core/loop_phases.py`:

- `think_phase(state, model, tool_registry, hooks, ...) -> ThinkResult` — handles model generation, token streaming, audit request building, reasoning-loop detection
- `execute_tools_phase(state, tool_registry, ...) -> tuple[list[ToolResult], list[ToolExecutionRecord]]` — handles tool execution loop, parse-error guard, cache_store resolution

`agent_loop` reduces to ~150 lines orchestrating: think → gate → act/observe → guards → compact → hooks.

**Risk**: Highest-risk change. Should be a separate PR with existing test coverage as safety net.

---

### Issue 5 (MEDIUM): Strict API version matching

**Root cause**: `PluginScanner._scan_one()` (`scanner.py:140`) uses exact string equality for API version, rejecting backward-compatible upgrades.

**Fix**: Semver-based compatibility:

```python
def _is_api_compatible(plugin_api: str, host_api: str) -> bool:
    if plugin_api == host_api:
        return True
    try:
        p_major, p_minor = map(int, plugin_api.split("."))
        h_major, h_minor = map(int, host_api.split("."))
    except (ValueError, AttributeError):
        return plugin_api == host_api
    return p_major == h_major and p_minor <= h_minor
```

---

### Issue 6 (MEDIUM): PYTHONPATH overwrites plugin custom value

**Root cause**: `_start_one` (`manager.py:153`) unconditionally sets `PYTHONPATH`, overwriting any value the plugin set via `manifest.runtime.env`.

**Fix**: Merge instead of overwrite:

```python
existing = env.get("PYTHONPATH", os.environ.get("PYTHONPATH", ""))
env["PYTHONPATH"] = f"{project_root}:{existing}" if existing else project_root
```

Check `env` (already resolved from manifest) before falling back to `os.environ`.

---

### Issue 7 (MEDIUM): Encapsulation violation

**Root cause**: `PluginSystem` (`__init__.py:115,129`) accesses `self._manager._processes` directly.

**Fix**: Add public method to `ProcessManager`:

```python
def get_processes(self) -> dict[str, PluginProcess]:
    return dict(self._processes)
```

`PluginSystem` uses `self._manager.get_processes()`.

---

### Issue 8 (MEDIUM): No backpressure on streaming plugin agent responses

**Root cause**: `ProxyAgent.run()` (`proxies.py:126-137`) accumulates all chunks without size limit.

**Fix**: Add 10 MB cap:

```python
MAX_RESPONSE_SIZE = 10 * 1024 * 1024

# In run(), within the streaming loop:
total_size += len(chunk.chunk.encode("utf-8"))
if total_size > MAX_RESPONSE_SIZE:
    raise PluginRPCError(-1, "Response exceeds max size")
```

---

### Issue 9 (LOW): Duplicate protocol error code definitions

**Root cause**: `src/plugin/protocol.py` defines `ErrorCodes` class; `src/plugin/sdk/protocol.py` defines same constants at module level.

**Fix**: Make `sdk/protocol.py` the single source of truth. Add missing constants (`TIMEOUT_ERROR`, `TOOL_NOT_FOUND`, `PLUGIN_CRASHED`). Delete `ErrorCodes` class and re-export from `sdk/protocol.py`.

---

### Issue 10 (LOW): Missing tool name deduplication in PluginRuntime

**Root cause**: `PluginRuntime.run()` (`sdk/runtime.py:135-139`) blindly appends tool names from capabilities.

**Fix**: Guard against duplicates:

```python
if cap.get("type") == "tool" and "name" in cap:
    name = cap["name"]
    if name not in self._tool_names:
        self._tool_names.append(name)
```

---

### Issue 11 (LOW): Overly broad `Any` type annotations

**Root cause**: `client: Any`, `tool_registry: Any` scattered across plugin and agent modules.

**Fix**: Define `Protocol` classes for key interfaces (e.g., `ToolRegistryLike`). Apply progressively — start with `proxies.py` and `registry.py` interfaces. No behavioral change.

---

## Implementation Phases

| Phase | Issues | Files | Risk |
|-------|--------|-------|------|
| 1 | #1, #2, #3 (HIGH) | `base.py`, `registry.py`, `manager.py` | Low |
| 2 | #5, #6, #7, #8 (MEDIUM, small) | `scanner.py`, `manager.py`, `registry.py`, `proxies.py` | Low |
| 3 | #9, #10, #11 (LOW) | `protocol.py`, `sdk/protocol.py`, `runtime.py`, `proxies.py` | Minimal |
| 4 | #4 (agent_loop decomposition) | `loop.py`, `loop_phases.py` (new) | Medium |

Phase 4 should be a separate PR with test coverage verification beforehand.
