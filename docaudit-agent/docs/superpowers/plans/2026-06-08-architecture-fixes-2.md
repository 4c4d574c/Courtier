# Architecture Fixes (Round 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix 11 architecture issues (3 HIGH, 5 MEDIUM, 3 LOW) across the plugin system, agent framework, and protocol layer.

**Architecture:** Four phases. Phases 1–3 are each a single PR against main; Phase 4 is a separate PR. Each phase is independently testable and does not depend on later phases.

**Tech Stack:** Python 3.13, asyncio, Pydantic, FastAPI (lifespan)

**Spec:** `docs/superpowers/specs/2026-06-08-architecture-fixes-2-design.md`

---

## File Map

| File | Phase | Action | Responsibility |
|------|-------|--------|----------------|
| `src/agent/agents/base.py` | 1 | Modify | Issue #1 — incremental tool sync |
| `src/agent/tools/registry.py` | 1 | Modify | Issue #2 — `force` parameter |
| `src/plugin/manager.py` | 1, 2 | Modify | Issues #3, #6, #7 — crash breaker, PYTHONPATH, public API |
| `src/plugin/registry.py` | 2 | Modify | Issue #2 (caller side) — use `force=True` |
| `src/plugin/scanner.py` | 2 | Modify | Issue #5 — semver compatibility |
| `src/plugin/proxies.py` | 2, 3 | Modify | Issues #8, #11 — backpressure, Protocol types |
| `src/plugin/__init__.py` | 3 | Modify | Issue #7 (caller side), #9 (export cleanup) |
| `src/plugin/protocol.py` | 3 | Modify | Issue #9 — remove ErrorCodes class |
| `src/plugin/sdk/protocol.py` | 3 | Modify | Issue #9 — add missing constants |
| `src/plugin/sdk/runtime.py` | 3 | Modify | Issue #10 — deduplicate tool names |
| `src/agent/core/loop_phases.py` | 4 | **Create** | Issue #4 — extracted think + execute phases |
| `src/agent/core/loop.py` | 4 | Modify | Issue #4 — reduced to ~150-line orchestrator |

---

## Phase 1 — HIGH Issues (#1, #2, #3)

### Task 1.1: Add `force` parameter to ToolRegistry.register()

**Files:**
- Modify: `src/agent/tools/registry.py:44-48`

- [ ] **Step 1: Update `register()` signature and body**

Replace lines 44-48:

```python
    def register(self, tool: ToolProtocol) -> None:
        """Register a tool. Raises ValueError on duplicate name."""
        if tool.name in self._tools:
            raise ValueError(f"Duplicate tool name: {tool.name}")
        self._tools[tool.name] = tool
```

With:

```python
    def register(self, tool: ToolProtocol, force: bool = False) -> None:
        """Register a tool. Raises ValueError on duplicate name unless force=True."""
        if tool.name in self._tools:
            if force:
                del self._tools[tool.name]
            else:
                raise ValueError(f"Duplicate tool name: {tool.name}")
        self._tools[tool.name] = tool
```

- [ ] **Step 2: Commit**

```bash
git add src/agent/tools/registry.py
git commit -m "feat: add force parameter to ToolRegistry.register for explicit overwrite"
```

---

### Task 1.2: Fix Agent ToolRegistry snapshot timing

**Files:**
- Modify: `src/agent/agents/base.py:131-141` (constructor), add sync block in `run()` before `agent_loop` call

- [ ] **Step 1: Store shared registry reference in `__init__`**

Replace lines 131-141:

```python
        # Use provided shared registry as base, or create new one.
        # Plugin tools registered in a shared ToolRegistry are copied so
        # the agent sees them without sharing per-run state counters.
        if tool_registry is not None:
            self.tool_registry = ToolRegistry()
            for tool in tool_registry.list_tools():
                self.tool_registry.register(tool)
        else:
            self.tool_registry = ToolRegistry()
        for tool in tools or []:
            self.tool_registry.register(tool)
```

With:

```python
        # Use provided shared registry as base, or create new one.
        # Plugin tools are registered into the shared registry at PluginSystem
        # startup time (FastAPI lifespan), which happens after Agent construction.
        # Keep a reference and sync incrementally on each run() to pick up
        # late-registered plugin tools while preserving per-run state isolation.
        if tool_registry is not None:
            self._shared_tool_registry = tool_registry
            self.tool_registry = ToolRegistry()
            for tool in tool_registry.list_tools():
                self.tool_registry.register(tool)
        else:
            self._shared_tool_registry = None
            self.tool_registry = ToolRegistry()
        for tool in tools or []:
            self.tool_registry.register(tool)
```

- [ ] **Step 2: Add incremental sync at start of `run()`**

In `Agent.run()`, after the `if task is None: raise ValueError(...)` guard (line 218-219), insert before line 221 (`system_prompt = ...`):

```python
        # Incremental sync: pull late-registered tools from the shared registry
        # (e.g., plugin tools registered during FastAPI lifespan after Agent init).
        if self._shared_tool_registry is not None:
            existing = {t.name for t in self.tool_registry.list_tools()}
            for tool in self._shared_tool_registry.list_tools():
                if tool.name not in existing:
                    self.tool_registry.register(tool)
```

- [ ] **Step 3: Run existing tests to verify no regression**

```bash
uv run pytest src/agent/ -x -q 2>&1 | tail -20
```

Expected: all tests pass (no new failures).

- [ ] **Step 4: Commit**

```bash
git add src/agent/agents/base.py
git commit -m "fix: incremental-sync shared ToolRegistry on each Agent.run() to pick up plugin tools"
```

---

### Task 1.3: Add immediate-recrash circuit breaker

**Files:**
- Modify: `src/plugin/manager.py:44-56` (PluginProcess dataclass), `:196` (record started_at), `:261-307` (_on_crash)

- [ ] **Step 1: Add `_started_at` field to PluginProcess**

In `PluginProcess` dataclass (after line 56, before the `client` property), add:

```python
    _started_at: float = field(default=0.0, repr=False)
```

- [ ] **Step 2: Record startup timestamp when plugin reaches ACTIVE**

After line 197 (`proc._restart_count = 0`), add:

```python
        proc._started_at = asyncio.get_event_loop().time()
```

- [ ] **Step 3: Define crash-window constant at module level**

After line 19 (`_ENV_REF_RE`), add:

```python
# Window (seconds) after reaching ACTIVE within which a crash is treated as
# a deterministic startup failure → FATAL with no restart attempts.
_IMMEDIATE_CRASH_WINDOW = 5.0
```

- [ ] **Step 4: Add immediate-crash guard in `_on_crash`**

In `_on_crash`, after the early-return guard (line 264) and before `proc.state = PluginState.CRASHED` (line 266), insert:

```python
        # Circuit breaker: if the plugin crashed within seconds of reaching
        # ACTIVE, it's a deterministic startup failure — skip restart.
        if proc._started_at > 0:
            uptime = asyncio.get_event_loop().time() - proc._started_at
            if uptime < _IMMEDIATE_CRASH_WINDOW:
                proc.state = PluginState.FATAL
                logger.error(
                    "Plugin '%s' crashed %.1fs after startup (< %.0fs window), marking FATAL",
                    proc.name, uptime, _IMMEDIATE_CRASH_WINDOW,
                )
                return
```

- [ ] **Step 5: Verify syntax and imports**

```bash
uv run python -c "from src.plugin.manager import ProcessManager, PluginProcess, PluginState; print('OK')"
```

Expected: `OK`

- [ ] **Step 6: Commit**

```bash
git add src/plugin/manager.py
git commit -m "fix: add immediate-recrash circuit breaker to plugin crash recovery"
```

---

### Task 1.4: Phase 1 final verification

- [ ] **Step 1: Run full test suite**

```bash
uv run pytest src/ -x -q --ignore=src/docparse/tests --ignore=src/docbuilder/tests --ignore=src/validator/tests --ignore=src/docannot/tests 2>&1 | tail -20
```

Expected: all tests pass.

- [ ] **Step 2: Push Phase 1**

```bash
git push origin main
```

---

## Phase 2 — MEDIUM Issues (#5, #6, #7, #8) + Issue #2 caller update

### Task 2.1: Update ExtensionRegistry to use `force=True`

**Files:**
- Modify: `src/plugin/registry.py:138-148`

- [ ] **Step 1: Simplify `_register_tool` to use `force=True`**

Replace lines 138-148:

```python
        proxy = ProxyTool(client, cap)
        try:
            self._tool_registry.register(proxy)
        except ValueError:
            logger.warning(
                "Duplicate tool name '%s' from plugin '%s', overwriting",
                cap["name"],
                plugin_name,
            )
            self._tool_registry.unregister(cap["name"])
            self._tool_registry.register(proxy)
```

With:

```python
        proxy = ProxyTool(client, cap)
        self._tool_registry.register(proxy, force=True)
```

- [ ] **Step 2: Commit**

```bash
git add src/plugin/registry.py
git commit -m "refactor: use force=True in ExtensionRegistry._register_tool instead of try/except"
```

---

### Task 2.2: Semver API version compatibility check

**Files:**
- Modify: `src/plugin/scanner.py:139-147`

- [ ] **Step 1: Add `_is_api_compatible` helper function**

After the class-level constant `HOST_API_VERSION = "1.0"` (line 40 in scanner.py), add:

```python
    @staticmethod
    def _is_api_compatible(plugin_api: str, host_api: str) -> bool:
        """Check semver compatibility: same major, plugin minor <= host minor."""
        if plugin_api == host_api:
            return True
        try:
            p_major, p_minor = map(int, plugin_api.split("."))
            h_major, h_minor = map(int, host_api.split("."))
        except (ValueError, AttributeError):
            return plugin_api == host_api
        return p_major == h_major and p_minor <= h_minor
```

- [ ] **Step 2: Replace strict version check**

Replace lines 139-146:

```python
        # Validate: API version compatible
        if manifest.api != self.HOST_API_VERSION:
            return PluginScanResult(
                name=manifest.name,
                dir=plugin_dir,
                status=ScanStatus.BLOCKED,
                error=f"API version mismatch: plugin requires '{manifest.api}', host is '{self.HOST_API_VERSION}'",
            )
```

With:

```python
        # Validate: API version compatible (semver: same major, plugin minor <= host minor)
        if not self._is_api_compatible(manifest.api, self.HOST_API_VERSION):
            return PluginScanResult(
                name=manifest.name,
                dir=plugin_dir,
                status=ScanStatus.BLOCKED,
                error=f"API version incompatible: plugin requires '{manifest.api}', host is '{self.HOST_API_VERSION}'",
            )
```

- [ ] **Step 3: Verify syntax**

```bash
uv run python -c "from src.plugin.scanner import PluginScanner; s = PluginScanner(); print(s._is_api_compatible('1.0', '1.0')); print(s._is_api_compatible('1.0', '1.1')); print(s._is_api_compatible('1.1', '1.0')); print(s._is_api_compatible('2.0', '1.0'))"
```

Expected: `True`, `True`, `False`, `False`

- [ ] **Step 4: Commit**

```bash
git add src/plugin/scanner.py
git commit -m "feat: use semver-based API version compatibility check in PluginScanner"
```

---

### Task 2.3: Merge (don't overwrite) plugin PYTHONPATH

**Files:**
- Modify: `src/plugin/manager.py:149-153`

- [ ] **Step 1: Fix PYTHONPATH resolution to preserve plugin env**

Replace lines 149-153:

```python
        # Add project root to PYTHONPATH so plugins can import from src.*
        # proc.plugin_dir = {project_root}/plugins/{name}
        project_root = str(proc.plugin_dir.parent.parent.resolve())
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = f"{project_root}:{existing}" if existing else project_root
```

With:

```python
        # Add project root to PYTHONPATH so plugins can import from src.*
        # proc.plugin_dir = {project_root}/plugins/{name}
        # Merge with any PYTHONPATH the plugin already set via manifest.runtime.env
        # (resolved in the env dict above), falling back to os.environ.
        project_root = str(proc.plugin_dir.parent.parent.resolve())
        existing = env.get("PYTHONPATH") or os.environ.get("PYTHONPATH", "")
        env["PYTHONPATH"] = f"{project_root}:{existing}" if existing else project_root
```

- [ ] **Step 2: Commit**

```bash
git add src/plugin/manager.py
git commit -m "fix: merge PYTHONPATH instead of overwriting plugin-configured value"
```

---

### Task 2.4: Add public `get_processes()` to ProcessManager

**Files:**
- Modify: `src/plugin/manager.py` (ProcessManager class)
- Modify: `src/plugin/__init__.py:114-116,129-134`

- [ ] **Step 1: Add `get_processes()` method to ProcessManager**

After `__init__` in ProcessManager (after line 93), add:

```python
    def get_processes(self) -> dict[str, PluginProcess]:
        """Return a copy of the process map keyed by plugin name."""
        return dict(self._processes)
```

- [ ] **Step 2: Update PluginSystem to use public method**

In `src/plugin/__init__.py`, replace line 115:

```python
        for name, proc in self._manager._processes.items():
```

With:

```python
        for name, proc in self._manager.get_processes().items():
```

Replace lines 129-134:

```python
        for name, proc in self._manager._processes.items():
```

With:

```python
        for name, proc in self._manager.get_processes().items():
```

- [ ] **Step 3: Verify syntax**

```bash
uv run python -c "from src.plugin import PluginSystem; print('OK')"
```

Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add src/plugin/manager.py src/plugin/__init__.py
git commit -m "refactor: add public ProcessManager.get_processes() and use it in PluginSystem"
```

---

### Task 2.5: Add streaming response size cap to ProxyAgent

**Files:**
- Modify: `src/plugin/proxies.py:16` (add constant), `:126-137` (run loop)

- [ ] **Step 1: Define MAX_RESPONSE_SIZE constant**

After the logger line (line 13), add:

```python
# Maximum total response size (bytes) for streaming agent.run() responses
# to prevent OOM from a misbehaving plugin.
_MAX_RESPONSE_SIZE = 10 * 1024 * 1024  # 10 MB
```

- [ ] **Step 2: Add size tracking in ProxyAgent.run()**

In `ProxyAgent.run()`, replace lines 126-137:

```python
        content_parts: list[str] = []
        final_result: dict[str, Any] = {}

        async for chunk in self._client.stream("agent.run", {
            "agent": self._agent_name,
            "task": task,
            **{k: v for k, v in kwargs.items() if k not in self._HOST_KWARGS},
        }):
            if chunk.chunk is not None:
                content_parts.append(chunk.chunk)
            if chunk.is_end and chunk.result is not None:
                final_result = chunk.result if isinstance(chunk.result, dict) else {}
```

With:

```python
        content_parts: list[str] = []
        final_result: dict[str, Any] = {}
        total_size = 0

        async for chunk in self._client.stream("agent.run", {
            "agent": self._agent_name,
            "task": task,
            **{k: v for k, v in kwargs.items() if k not in self._HOST_KWARGS},
        }):
            if chunk.chunk is not None:
                total_size += len(chunk.chunk.encode("utf-8"))
                if total_size > _MAX_RESPONSE_SIZE:
                    raise PluginRPCError(
                        -1,
                        f"Agent '{self._agent_name}' response exceeds max size ({_MAX_RESPONSE_SIZE} bytes)",
                    )
                content_parts.append(chunk.chunk)
            if chunk.is_end and chunk.result is not None:
                final_result = chunk.result if isinstance(chunk.result, dict) else {}
```

- [ ] **Step 3: Verify syntax**

```bash
uv run python -c "from src.plugin.proxies import ProxyAgent; print('OK')"
```

Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add src/plugin/proxies.py
git commit -m "fix: add 10MB response size cap to ProxyAgent streaming to prevent OOM"
```

---

### Task 2.6: Phase 2 final verification

- [ ] **Step 1: Run full test suite**

```bash
uv run pytest src/ -x -q --ignore=src/docparse/tests --ignore=src/docbuilder/tests --ignore=src/validator/tests --ignore=src/docannot/tests 2>&1 | tail -20
```

Expected: all tests pass.

- [ ] **Step 2: Push Phase 2**

```bash
git push origin main
```

---

## Phase 3 — LOW Issues (#9, #10, #11)

### Task 3.1: Consolidate protocol error codes (Issue #9)

**Files:**
- Modify: `src/plugin/sdk/protocol.py` (add missing constants)
- Modify: `src/plugin/protocol.py:10-19` (remove ErrorCodes class)
- Modify: `src/plugin/__init__.py:34,54` (remove ErrorCodes from exports)

- [ ] **Step 1: Add missing error codes to sdk/protocol.py**

After line 18 (`INTERNAL_ERROR = -32603`) in `src/plugin/sdk/protocol.py`, add:

```python
TIMEOUT_ERROR = -32000
TOOL_NOT_FOUND = -32001
PLUGIN_CRASHED = -32002
```

- [ ] **Step 2: Remove ErrorCodes class from host protocol.py**

Replace lines 10-19 in `src/plugin/protocol.py`:

```python
class ErrorCodes:
    """JSON-RPC error code constants."""

    PARSE_ERROR = -32700
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603
    TIMEOUT_ERROR = -32000
    TOOL_NOT_FOUND = -32001
    PLUGIN_CRASHED = -32002
```

With:

```python
# Error code constants are now defined in sdk/protocol.py (single source of truth).
# Re-exported here for backward compatibility.
from .sdk.protocol import (  # noqa: F401
    PARSE_ERROR,
    METHOD_NOT_FOUND,
    INVALID_PARAMS,
    INTERNAL_ERROR,
    TIMEOUT_ERROR,
    TOOL_NOT_FOUND,
    PLUGIN_CRASHED,
)
```

- [ ] **Step 3: Remove ErrorCodes from __init__.py exports**

In `src/plugin/__init__.py`:

Remove `ErrorCodes` from the import block (line 34):
```python
# Before: ... ErrorCodes,
# After: (remove this line)
```

Remove `"ErrorCodes",` from `__all__` list (line 54).

- [ ] **Step 4: Verify syntax and imports**

```bash
uv run python -c "from src.plugin.sdk.protocol import TIMEOUT_ERROR, TOOL_NOT_FOUND, PLUGIN_CRASHED; print(TIMEOUT_ERROR, TOOL_NOT_FOUND, PLUGIN_CRASHED)"
```

Expected: `-32000 -32001 -32002`

```bash
uv run python -c "from src.plugin.protocol import PARSE_ERROR, TIMEOUT_ERROR; print(PARSE_ERROR, TIMEOUT_ERROR)"
```

Expected: `-32700 -32000`

- [ ] **Step 5: Commit**

```bash
git add src/plugin/sdk/protocol.py src/plugin/protocol.py src/plugin/__init__.py
git commit -m "refactor: consolidate protocol error codes in sdk/protocol.py as single source of truth"
```

---

### Task 3.2: Deduplicate tool names in PluginRuntime (Issue #10)

**Files:**
- Modify: `src/plugin/sdk/runtime.py:135-139`

- [ ] **Step 1: Guard against duplicate tool/checker names**

Replace lines 135-139:

```python
        for cap in caps:
            if cap.get("type") == "tool" and "name" in cap:
                self._tool_names.append(cap["name"])
            elif cap.get("type") == "checker" and "name" in cap:
                self._checker_names.append(cap["name"])
```

With:

```python
        for cap in caps:
            if cap.get("type") == "tool" and "name" in cap:
                name = cap["name"]
                if name not in self._tool_names:
                    self._tool_names.append(name)
            elif cap.get("type") == "checker" and "name" in cap:
                name = cap["name"]
                if name not in self._checker_names:
                    self._checker_names.append(name)
```

- [ ] **Step 2: Commit**

```bash
git add src/plugin/sdk/runtime.py
git commit -m "fix: deduplicate tool and checker names in PluginRuntime"
```

---

### Task 3.3: Add Protocol types for key plugin interfaces (Issue #11)

**Files:**
- Modify: `src/plugin/proxies.py` (add Protocol, use in signatures)

- [ ] **Step 1: Define Protocol classes at top of proxies.py**

After the imports (after line 13), add:

```python
from typing import Protocol, runtime_checkable


@runtime_checkable
class _JSONRPCClientLike(Protocol):
    """Structural interface for JSONRPCClient — avoids circular imports."""
    plugin_name: str

    async def call(self, method: str, params: dict[str, Any] | None = None, timeout: float = 30.0) -> Any: ...
    def stream(self, method: str, params: dict[str, Any] | None = None, heartbeat_timeout: float = 60.0) -> AsyncIterator: ...


@runtime_checkable
class _ToolRegistryLike(Protocol):
    """Structural interface for ToolRegistry."""
    def register(self, tool: Any, force: bool = False) -> None: ...
    def unregister(self, name: str) -> None: ...
    def list_tools(self) -> list[Any]: ...
```

- [ ] **Step 2: Update ProxyTool, ProxyChecker, ProxyAgent, ProxyRoute signatures**

Replace `client: Any` with `client: _JSONRPCClientLike` in:
- `ProxyTool.__init__` (line 23)
- `ProxyChecker.__init__` (line 61)
- `ProxyAgent.__init__` (line 107)

- [ ] **Step 3: Verify syntax**

```bash
uv run python -c "from src.plugin.proxies import ProxyTool, ProxyChecker, ProxyAgent, ProxyRoute; print('OK')"
```

Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add src/plugin/proxies.py
git commit -m "refactor: add Protocol types for JSONRPCClient and ToolRegistry interfaces"
```

---

### Task 3.4: Phase 3 final verification

- [ ] **Step 1: Run full test suite**

```bash
uv run pytest src/ -x -q --ignore=src/docparse/tests --ignore=src/docbuilder/tests --ignore=src/validator/tests --ignore=src/docannot/tests 2>&1 | tail -20
```

Expected: all tests pass.

- [ ] **Step 2: Push Phase 3**

```bash
git push origin main
```

---

## Phase 4 — agent_loop Decomposition (Issue #4, Separate PR)

> **⚠️ Branch first.** This is the highest-risk change. Work on a feature branch.

### Task 4.1: Create feature branch

```bash
git checkout -b refactor/agent-loop-decomposition
```

### Task 4.2: Extract `think_phase` to `loop_phases.py`

**Files:**
- Create: `src/agent/core/loop_phases.py`
- Modify: `src/agent/core/loop.py`

- [ ] **Step 1: Create loop_phases.py with think_phase**

Write `src/agent/core/loop_phases.py`:

```python
"""Extracted phases from agent_loop — think and tool-execute."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

from .audit_logger import LLMRequestRecord, LLMResponseRecord
from .loop_guards import detect_reasoning_loop
from .loop_streaming import generate_with_streaming_fallback

if TYPE_CHECKING:
    from .model import ModelClient
    from .state import AgentState
    from ..tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


@dataclass
class ThinkResult:
    """Output of the think phase."""
    state: "AgentState"
    llm_request: LLMRequestRecord
    llm_response: LLMResponseRecord
    llm_duration_ms: int
    recent_reasoning: list[str]
    tokens_streamed: bool


async def think_phase(
    *,
    state: "AgentState",
    model: "ModelClient",
    tool_registry: "ToolRegistry | None",
    context_manager: Any,
    recent_reasoning: list[str],
    on_step: Any,
    on_token: Any,
    on_content_token: Any,
) -> ThinkResult:
    """Think phase: compact context, call model, detect reasoning loops.

    Returns a ThinkResult. The caller checks whether the state became
    terminal (model returned no tool calls, or reasoning loop detected).
    """
    turn_index = state.current_step

    # --- Context budget: Layer 3 full compaction before think ---
    if context_manager:
        state = state.model_copy(
            update={
                "messages": await context_manager.compact_if_needed(state.messages)
            }
        )
        if state.is_terminal():
            return ThinkResult(
                state=state,
                llm_request=LLMRequestRecord(messages=[], tools=None, model="", temperature=None),
                llm_response=LLMResponseRecord(content=None, reasoning=None, tool_calls=[], usage=None, finish_reason="error", duration_ms=0),
                llm_duration_ms=0,
                recent_reasoning=recent_reasoning,
                tokens_streamed=False,
            )

    # THINK: model generates next action
    messages = state.to_openai_messages()
    tools_schemas = (
        tool_registry.get_schemas(hide_debug_for_task_agents=True)
        if tool_registry else None
    )
    model_name = getattr(model, "model_name", "unknown")
    temperature = getattr(model, "_temperature", None)

    llm_request = LLMRequestRecord(
        messages=list(messages),
        tools=tools_schemas,
        model=model_name,
        temperature=temperature,
    )

    llm_start = time.perf_counter()
    tokens_streamed = False

    if on_token is not None:
        response, tokens_streamed = await generate_with_streaming_fallback(
            model=model,
            messages=messages,
            tools=tools_schemas,
            on_token=on_token,
            on_content_token=on_content_token,
        )
        if tokens_streamed and on_step:
            await on_step("stream", "streaming_response")
    else:
        response = await model.generate(messages, tools=tools_schemas)

    llm_duration_ms = int((time.perf_counter() - llm_start) * 1000)

    llm_response = LLMResponseRecord(
        content=response.content,
        reasoning=response.reasoning_content,
        tool_calls=[
            {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
            for tc in response.tool_calls
        ],
        usage=response.usage,
        finish_reason=response.finish_reason,
        duration_ms=llm_duration_ms,
    )

    # Log reasoning content
    if response.reasoning_content:
        logger.info(
            "Turn %d reasoning (%d chars): %s",
            turn_index,
            len(response.reasoning_content),
            response.reasoning_content[:600] + ("..." if len(response.reasoning_content) > 600 else ""),
        )

    # Fire usage event
    if response.usage and on_step:
        await on_step("usage", f"{response.usage['prompt_tokens']},{response.usage['completion_tokens']}")

    if on_step:
        if response.tool_calls:
            names = ", ".join(tc.name for tc in response.tool_calls)
            await on_step("think", f"tool_calls: {names}")
        elif not tokens_streamed:
            await on_step("think", "text_response")

    state = state.add_thought(response)

    # Detect reasoning loop
    if response.reasoning_content:
        recent_reasoning.append(response.reasoning_content)
        if detect_reasoning_loop(recent_reasoning):
            logger.warning(
                "Reasoning loop detected (%d consecutive near-duplicate steps). Forcing completion.",
                3,
            )
            state = state.model_copy(
                update={
                    "status": "completed",
                    "termination_reason": "reasoning_loop_detected",
                }
            )

    return ThinkResult(
        state=state,
        llm_request=llm_request,
        llm_response=llm_response,
        llm_duration_ms=llm_duration_ms,
        recent_reasoning=recent_reasoning,
        tokens_streamed=tokens_streamed,
    )
```

- [ ] **Step 2: Extract execute_tools_phase to loop_phases.py**

Append to `src/agent/core/loop_phases.py`:

```python
async def execute_tools_phase(
    *,
    state: "AgentState",
    tool_registry: "ToolRegistry",
    context_manager: Any,
    artifact_store: Any,
    on_tool_result: Any,
) -> tuple[list[Any], list[Any]]:
    """Execute all tool calls in the current state.

    Returns: (results: list[ToolResult], records: list[ToolExecutionRecord])
    """
    from .audit_logger import ToolExecutionRecord
    from .loop_utils import tool_result_summary
    from ..tools.protocol import ToolResult

    results: list[ToolResult] = []
    records: list[ToolExecutionRecord] = []

    for tool_call in state.tool_calls:
        tool_start = time.perf_counter()

        if "_parse_error" in tool_call.arguments:
            raw_args = tool_call.arguments.get("raw", "")[:200]
            result = ToolResult(
                success=False,
                error=(
                    f"Failed to parse arguments for tool '{tool_call.name}'. "
                    f"Raw arguments: {raw_args}"
                ),
            )
            tool_duration_ms = int((time.perf_counter() - tool_start) * 1000)
            records.append(
                ToolExecutionRecord(
                    tool_name=tool_call.name,
                    tool_call_id=tool_call.id,
                    arguments=dict(tool_call.arguments),
                    result_success=result.success,
                    result_data=result.data,
                    result_error=result.error,
                    duration_ms=tool_duration_ms,
                )
            )
            results.append(result)
            continue

        try:
            cache_store = getattr(context_manager, '_cache', None) if context_manager else None
            result = await tool_registry.execute(
                tool_call.name,
                context_manager=context_manager,
                cache_store=cache_store,
                artifact_store=artifact_store,
                **tool_call.arguments,
            )
        except Exception as exc:
            logger.exception("Tool %s failed", tool_call.name)
            result = ToolResult(success=False, error=str(exc))

        tool_duration_ms = int((time.perf_counter() - tool_start) * 1000)

        if on_tool_result:
            summary = tool_result_summary(result)
            await on_tool_result(tool_call.name, result, summary)

        records.append(
            ToolExecutionRecord(
                tool_name=tool_call.name,
                tool_call_id=tool_call.id,
                arguments=dict(tool_call.arguments),
                result_success=result.success,
                result_data=result.data,
                result_error=result.error,
                duration_ms=tool_duration_ms,
            )
        )
        results.append(result)

    return results, records
```

The `tool_result_summary` helper is imported from the existing `src/agent/core/loop_utils.py` module.

- [ ] **Step 3: Rewrite agent_loop to use extracted phases**

Replace the body of `agent_loop` in `src/agent/core/loop.py` (lines 78-450, keeping the function signature and early setup, replacing from the while loop):

The new `agent_loop` is approximately:

```python
    while not current_state.is_terminal():
        turn_index = current_state.current_step
        timestamp = time.time()

        # Hook: pre_think
        if hooks:
            current_state = await hooks.run("pre_think", current_state)
            if current_state.is_terminal():
                break

        # --- Think phase ---
        try:
            think = await think_phase(
                state=current_state,
                model=model,
                tool_registry=tool_registry,
                context_manager=context_manager,
                recent_reasoning=recent_reasoning,
                on_step=on_step,
                on_token=on_token,
                on_content_token=on_content_token,
            )
        except Exception as exc:
            logger.exception("Model generation failed")
            if audit_logger:
                error_response = LLMResponseRecord(
                    content=None, reasoning=None, tool_calls=[],
                    usage=None, finish_reason="error", duration_ms=0,
                )
                write_audit_turn(audit_logger, turn_index, timestamp,
                                 LLMRequestRecord(messages=[], tools=None, model="", temperature=None),
                                 error_response)
                audit_logger.finalize("error", f"Model error: {exc}")
            return current_state.errored(f"Model error: {exc}")

        current_state = think.state
        recent_reasoning = think.recent_reasoning
        llm_request = think.llm_request
        llm_response = think.llm_response

        # If model returned no tool calls or reasoning loop was detected
        if current_state.is_terminal():
            if audit_logger:
                write_audit_turn(audit_logger, turn_index, timestamp, llm_request, llm_response)
            break

        # GATE: permission check
        if permissions:
            for tool_call in current_state.tool_calls:
                if not permissions.allow(tool_call):
                    current_state = current_state.blocked(f"Permission denied: {tool_call.name}")
                    break

        if current_state.is_terminal():
            if audit_logger:
                write_audit_turn(audit_logger, turn_index, timestamp, llm_request, llm_response)
            break

        # ACT + OBSERVE
        if current_state.tool_calls and tool_registry is None:
            return current_state.errored("Tool calls requested but no tool registry configured")

        if on_step:
            names = ", ".join(tc.name for tc in current_state.tool_calls)
            await on_step("act", f"executing: {names}")

        results, tool_records = await execute_tools_phase(
            state=current_state,
            tool_registry=tool_registry,
            context_manager=context_manager,
            artifact_store=artifact_store,
            on_tool_result=on_tool_result,
        )

        current_state = current_state.add_observation(tuple(results))

        # Guard: business artifact no-progress
        initial_business_count, turns_since_last_business_artifact, should_terminate = \
            check_business_artifact_progress(
                artifact_store, initial_business_count, turns_since_last_business_artifact
            )
        if should_terminate:
            current_state = current_state.model_copy(
                update={"status": "completed", "termination_reason": "no_business_artifact_progress"}
            )
            if on_step:
                await on_step("loop", "no_business_artifact_progress")
            if audit_logger:
                write_audit_turn(audit_logger, turn_index, timestamp, llm_request, llm_response, tuple(tool_records))
            break

        # Terminal-tool readiness guard
        current_state = check_and_inject_hints(
            tool_registry=tool_registry, artifact_store=artifact_store,
            consecutive_exploratory=consecutive_exploratory, current_state=current_state,
        )
        if current_state.is_terminal():
            if on_step:
                await on_step("loop", current_state.termination_reason or "hints_terminated")
            if audit_logger:
                write_audit_turn(audit_logger, turn_index, timestamp, llm_request, llm_response, tuple(tool_records))
            break

        # Explore-loop guard
        terminated = check_explore_loop(
            current_state, recent_null_results, recent_tool_calls_history, consecutive_exploratory,
        )
        if terminated:
            update_null_tracking(results, recent_null_results)
            update_tool_call_history(current_state.tool_calls, recent_tool_calls_history)
            consecutive_exploratory = update_exploratory_tracking(current_state.tool_calls, consecutive_exploratory)
            if on_step:
                await on_step("loop", "explore_loop_detected")
            if audit_logger:
                write_audit_turn(audit_logger, turn_index, timestamp, llm_request, llm_response, tuple(tool_records))
            break

        update_null_tracking(results, recent_null_results)
        update_tool_call_history(current_state.tool_calls, recent_tool_calls_history)
        consecutive_exploratory = update_exploratory_tracking(current_state.tool_calls, consecutive_exploratory)

        # Terminal tool called event
        if consecutive_exploratory == 0 and artifact_store is not None and tool_registry is not None:
            ready_tools_set = set(_get_ready_terminal_tools(tool_registry, artifact_store))
            called_terminal = [tc.name for tc in current_state.tool_calls if tc.name in ready_tools_set]
            if called_terminal:
                emit_event("terminal_tool_called", {"tools": called_terminal})

        # Audit log
        if audit_logger:
            write_audit_turn(audit_logger, turn_index, timestamp, llm_request, llm_response, tuple(tool_records))

        # Micro-compact
        if context_manager:
            compacted_messages = context_manager.micro_compact(current_state.messages)
            current_state = current_state.model_copy(update={"messages": compacted_messages})

        if on_step:
            await on_step("observe", "results_collected")

        # Hook: post_observe
        if hooks:
            current_state = await hooks.run("post_observe", current_state)

    if audit_logger:
        audit_logger.finalize(final_status=current_state.status, termination_reason=current_state.termination_reason)

    return current_state
```

- [ ] **Step 4: Update imports in loop.py**

Replace the imports at the top to remove those that moved to loop_phases.py and add the new imports. Specifically, remove `LLMRequestRecord, LLMResponseRecord` from the audit_logger import (since these are now used only in think_phase), and add:

```python
from .loop_phases import think_phase, execute_tools_phase
```

Remove the `generate_with_streaming_fallback` import (moved to loop_phases.py).

Remove the `detect_reasoning_loop` import from loop_guards (moved to loop_phases.py).

- [ ] **Step 5: Run tests**

```bash
uv run pytest src/agent/ -x -q 2>&1 | tail -20
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/agent/core/loop_phases.py src/agent/core/loop.py
git commit -m "refactor: extract think_phase and execute_tools_phase from agent_loop"
```

---

### Task 4.3: Merge to main

```bash
git checkout main
git merge refactor/agent-loop-decomposition
git branch -d refactor/agent-loop-decomposition
git push origin main
```
