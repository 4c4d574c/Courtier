# Architecture & Module Design Fixes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix all 10 architecture and module design issues identified in code review — dual-track unification, service layer extraction, loop.py decomposition, oversized file splitting, and 6 medium issues.

**Architecture:** Refactor-only — no new features. All public APIs preserved via `__init__.py` re-exports. Phases 1-4 are independent and parallelizable. Phases 5-7 depend on earlier phases.

**Tech Stack:** Python 3.12+, FastAPI, Pydantic, pytest, uv

---

## File Structure Map

```
Before                              After
──────────────────────────────────────────────────────────────
src/agent/api/routes.py (595L)      src/agent/api/routes/ (3 files)
                                    src/agent/api/services/ (4 files)

src/agent/core/loop.py (858L)       src/agent/core/loop.py (~200L)
                                    src/agent/core/loop_guards.py (~200L)
                                    src/agent/core/loop_hints.py (~200L)
                                    src/agent/core/loop_utils.py (~100L)

src/docparse/parsers/               src/docparse/parsers/
  scanned_parser.py (1003L)           scanned/ (8 files)

src/validator/                      src/validator/
  template_service.py (880L)          templates/ (4 files)

src/agent/agents/                   src/agent/agents/
  subagent.py (781L)                  subagent/ (4 files)

src/config.py                       src/config.py (unchanged)
src/agent/api/app.py                src/agent/api/app.py (updated)
                                    src/agent/tools/domain/ (6 new files)
```

---

### Task 1.1: Remove module-level singleton `app = create_app()`

**Files:**
- Modify: `src/agent/api/app.py:67-68`

- [ ] **Step 1: Remove module-level app instance**

In `src/agent/api/app.py`, delete the last two lines:

```python
# Delete these two lines (lines 67-68):
# Module-level app instance for uvicorn: uvicorn src.agent.api.app:app
app = create_app()
```

- [ ] **Step 2: Verify the factory still works**

Run: `cd /home/lmwl/Documents/docaudit/docaudit-agent && uv run python -c "from src.agent.api.app import create_app; app = create_app(); print('Factory OK, routes:', len(app.routes))"`
Expected: prints "Factory OK, routes: N" (where N is a positive integer)

- [ ] **Step 3: Verify existing tests pass**

Run: `uv run pytest tests/agent/api/ -v --timeout=30 2>&1 | tail -20`
Expected: All tests pass (no import-time side effects from removing singleton)

- [ ] **Step 4: Commit**

```bash
git add src/agent/api/app.py
git commit -m "refactor: remove module-level app singleton, use factory only"
```

---

### Task 1.2: Add unified ToolRegistry to app.state

**Files:**
- Modify: `src/agent/api/app.py:50-58`

- [ ] **Step 1: Add ToolRegistry initialization to create_app()**

In `src/agent/api/app.py`, modify the `create_app()` function. After line 58 (`app.state.active_tasks`), add:

```python
    # Unified tool registry — populated at startup with domain tools + agent tools
    from ..tools.registry import ToolRegistry
    app.state.tool_registry = ToolRegistry()
```

The updated shared state section should read:

```python
    # Shared state
    app.state.settings = settings
    app.state.session_store = SessionStore(
        sessions_dir or str(Path(settings.cache_dir) / "sessions")
    )
    app.state.file_store = FileStore(
        str(Path(settings.upload_dir) / ".file_registry")
    )
    app.state.pause_event = asyncio.Event()
    app.state.active_tasks: dict[str, asyncio.Task] = {}
    app.state.tool_registry = ToolRegistry()
```

- [ ] **Step 2: Verify app creation still works**

Run: `uv run python -c "from src.agent.api.app import create_app; app = create_app(); print(hasattr(app.state, 'tool_registry'))"`
Expected: prints `True`

- [ ] **Step 3: Commit**

```bash
git add src/agent/api/app.py
git commit -m "refactor: add unified ToolRegistry to app.state for DI"
```

---

### Task 2.1: Extract loop_utils.py — pure utility functions

**Files:**
- Create: `src/agent/core/loop_utils.py`
- Modify: `src/agent/core/loop.py:543-593` (remove extracted functions)

- [ ] **Step 1: Create loop_utils.py**

Create `src/agent/core/loop_utils.py`:

```python
"""Pure utility functions for the agent loop — summaries, formatting, similarity."""

from __future__ import annotations

import difflib
import json
from typing import Any

from ..tools.protocol import ToolResult


def tool_result_summary(result: ToolResult) -> str:
    """Produce a short summary string for a tool result.

    Handles scalar, dict, list, str, and nested/dataframe-like types.
    For dicts, counts top-level keys and distinguishes persisted refs.
    Non-scalar dict values (nested dicts) are counted in sub-field totals.
    """
    if not result.success:
        return f"失败: {result.error or '未知错误'}"
    data = result.data
    if data is None:
        return "完成 (无返回数据)"
    if isinstance(data, dict):
        if data.get("__persisted_output__"):
            size = data.get("size_chars", 0)
            return f"完成 ({fmt_size(size)}, 已缓存)"
        scalar = sum(1 for v in data.values() if isinstance(v, (str, int, float)))
        nested = sum(1 for v in data.values() if isinstance(v, dict))
        list_vals = sum(1 for v in data.values() if isinstance(v, list))
        if nested or list_vals:
            parts: list[str] = [f"完成 ({len(data)} 个字段"]
            if scalar:
                parts.append(f", {scalar} 标量")
            if nested:
                parts.append(f", {nested} 嵌套对象")
            if list_vals:
                parts.append(f", {list_vals} 列表")
            parts.append(")")
            return "".join(parts)
        return f"完成 ({len(data)} 个字段)"
    if isinstance(data, list):
        return f"完成 ({len(data)} 项)"
    if isinstance(data, str):
        preview = data[:50].replace("\n", " ")
        if len(data) > 50:
            preview += "..."
        return preview
    if isinstance(data, (int, float, bool)):
        return f"完成 ({data})"
    return "完成"


def fmt_size(chars: int) -> str:
    """Format byte/char count into human-readable string."""
    if chars >= 10_000:
        return f"{chars // 1000}k 字符"
    if chars >= 1_000:
        return f"{chars / 1000:.1f}k 字符"
    return f"{chars} 字符"


def similarity(a: str, b: str) -> float:
    """Compute string similarity ratio using difflib (0.0–1.0)."""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def normalize_args_for_dedup(args: dict) -> str:
    """Return a canonical, normalized JSON key for duplicate-call detection.

    Strips values that are known to vary without semantic change:
    - ``label`` (display-only metadata)
    - ``_timestamp`` / ``_request_id`` (transient ids)
    """
    stripped = {k: v for k, v in args.items() if k not in ("label", "_timestamp", "_request_id")}
    return json.dumps(stripped, sort_keys=True, ensure_ascii=False)
```

- [ ] **Step 2: Update loop.py — replace extracted functions with imports**

In `src/agent/core/loop.py`:

Remove these functions (lines 543-700):
- `_tool_result_summary` (lines 543-584)
- `_fmt_size` (lines 587-592)
- `_similarity` (lines 595-601)
- `_normalize_args_for_dedup` (lines 679-687)

Add import at top:
```python
from .loop_utils import tool_result_summary, fmt_size, similarity, normalize_args_for_dedup
```

Update call sites in loop.py:
- `_tool_result_summary(result)` → `tool_result_summary(result)` (line 334)
- `_similarity(recent[i], recent[i+1])` → `similarity(recent[i], recent[i+1])` (line 235)
- `_normalize_args_for_dedup(dict(tc.arguments))` → `normalize_args_for_dedup(dict(tc.arguments))` (line 696)

- [ ] **Step 3: Verify tests pass**

Run: `uv run pytest tests/agent/test_loop.py -v --timeout=30 2>&1 | tail -20`
Expected: All tests pass

- [ ] **Step 4: Verify imports work**

Run: `uv run python -c "from src.agent.core.loop_utils import tool_result_summary, fmt_size, similarity, normalize_args_for_dedup; print('Imports OK')"`
Expected: prints "Imports OK"

- [ ] **Step 5: Commit**

```bash
git add src/agent/core/loop_utils.py src/agent/core/loop.py
git commit -m "refactor: extract loop_utils.py from loop.py"
```

---

### Task 2.2: Extract loop_guards.py — loop termination guards

**Files:**
- Create: `src/agent/core/loop_guards.py`
- Modify: `src/agent/core/loop.py`

- [ ] **Step 1: Create loop_guards.py**

Create `src/agent/core/loop_guards.py`:

```python
"""Loop termination guards — detect and prevent futile agent behavior."""

from __future__ import annotations

import logging
from typing import Any

from .state import AgentState
from ..artifacts.resolver import emit_event

logger = logging.getLogger(__name__)

# Max consecutive steps with near-duplicate reasoning before forcing termination.
_MAX_REASONING_DUPE_STEPS = 3
# Similarity ratio threshold (0.0–1.0).
_REASONING_SIMILARITY_THRESHOLD = 0.85
# Max consecutive tool calls that return null/empty results.
_MAX_NULL_TOOL_RESULTS = 5
# Max repeated calls to the same tool with same arguments.
_MAX_SAME_TOOL_CALLS = 3
# Max consecutive get_artifact / list_artifacts exploratory calls.
_MAX_CONSECUTIVE_EXPLORATORY = 8

_EXPLORATORY_TOOLS = frozenset({"get_artifact", "list_artifacts"})


def check_explore_loop(
    state: AgentState,
    null_results: list[bool],
    tool_calls_history: list[tuple[str, str]],
    consecutive_exploratory: int = 0,
) -> bool:
    """Detect futile exploration patterns and return True if terminal.

    Three patterns are caught:
    1. Consecutive null/empty tool results.
    2. Repeated calls to the same tool with identical arguments.
    3. Too many consecutive read-only/exploratory tool calls.
    """
    if state.is_terminal():
        return False

    # Pattern 1: too many consecutive null results
    if len(null_results) >= _MAX_NULL_TOOL_RESULTS and all(null_results[-_MAX_NULL_TOOL_RESULTS:]):
        logger.warning(
            "Explore-loop detected: %d consecutive null results. Forcing completion.",
            _MAX_NULL_TOOL_RESULTS,
        )
        emit_event("loop_no_progress_detected", {
            "reason": "null_results",
            "count": _MAX_NULL_TOOL_RESULTS,
        })
        return True

    # Pattern 2: repeated identical tool call
    if len(tool_calls_history) >= _MAX_SAME_TOOL_CALLS:
        recent = tool_calls_history[-_MAX_SAME_TOOL_CALLS:]
        if len(set(recent)) == 1:
            logger.warning(
                "Explore-loop detected: %d repeated calls to %s. Forcing completion.",
                _MAX_SAME_TOOL_CALLS,
                recent[0],
            )
            emit_event("loop_no_progress_detected", {
                "reason": "repeated_tool_call",
                "tool": recent[0][0],
                "count": _MAX_SAME_TOOL_CALLS,
            })
            return True

    # Pattern 3: too many consecutive exploratory tool calls
    if consecutive_exploratory >= _MAX_CONSECUTIVE_EXPLORATORY:
        logger.warning(
            "Explore-loop detected: %d consecutive exploratory tool calls. "
            "Forcing completion.",
            consecutive_exploratory,
        )
        emit_event("loop_no_progress_detected", {
            "reason": "consecutive_exploratory",
            "count": consecutive_exploratory,
        })
        return True

    return False


def detect_reasoning_loop(
    recent_reasoning: list[str],
) -> bool:
    """Check if the last N reasoning steps are near-duplicates.

    Import ``similarity`` from loop_utils at call time to avoid circular imports
    if needed; or import at module level.
    """
    from .loop_utils import similarity as _sim

    if len(recent_reasoning) < _MAX_REASONING_DUPE_STEPS:
        return False
    recent = recent_reasoning[-_MAX_REASONING_DUPE_STEPS:]
    return all(
        _sim(recent[i], recent[i + 1]) >= _REASONING_SIMILARITY_THRESHOLD
        for i in range(len(recent) - 1)
    )


def check_business_artifact_progress(
    artifact_store: Any,
    initial_business_count: int,
    turns_since_last_business_artifact: int,
) -> tuple[int, int, bool]:
    """Track business artifact production. Returns (new_initial_count, new_turns, should_terminate).

    When no new business artifacts are produced for too many turns, signals termination.
    """
    _MAX_TURNS_WITHOUT_BUSINESS_ARTIFACTS = 4

    if artifact_store is None:
        return initial_business_count, turns_since_last_business_artifact, False

    current_business = _count_business_artifacts(artifact_store)
    if current_business <= initial_business_count:
        turns_since_last_business_artifact += 1
    else:
        turns_since_last_business_artifact = 0
        initial_business_count = current_business

    if turns_since_last_business_artifact >= _MAX_TURNS_WITHOUT_BUSINESS_ARTIFACTS:
        logger.warning(
            "No-progress detected: %d turns without new business artifacts. "
            "Forcing completion.",
            turns_since_last_business_artifact,
        )
        emit_event("loop_no_progress_detected", {
            "reason": "no_business_artifacts",
            "turns": turns_since_last_business_artifact,
        })
        return initial_business_count, turns_since_last_business_artifact, True

    return initial_business_count, turns_since_last_business_artifact, False


def update_null_tracking(results: list, null_results: list[bool]) -> None:
    """Track whether results are null/empty for explore-loop detection."""
    _MAX_NULL_TOOL_RESULTS = 5
    for r in results:
        is_null = (
            r.data is None
            or (isinstance(r.data, dict) and not r.data)
            or (isinstance(r.data, list) and not r.data)
        )
        null_results.append(is_null)
    while len(null_results) > _MAX_NULL_TOOL_RESULTS * 2:
        null_results.pop(0)


def update_tool_call_history(
    tool_calls: tuple, history: list[tuple[str, str]]
) -> None:
    """Track tool call (name, args_key) for repeated-call detection."""
    _MAX_SAME_TOOL_CALLS = 3
    from .loop_utils import normalize_args_for_dedup

    for tc in tool_calls:
        args_key = normalize_args_for_dedup(dict(tc.arguments))
        history.append((tc.name, args_key))
    while len(history) > _MAX_SAME_TOOL_CALLS * 3:
        history.pop(0)


def update_exploratory_tracking(
    tool_calls: tuple, consecutive_exploratory: int,
) -> int:
    """Track consecutive exploratory (read-only) tool calls.

    Resets to 0 when a substantive tool call is made.
    """
    _EXPLORATORY_TOOLS = frozenset({"get_artifact", "list_artifacts"})
    if not tool_calls:
        return consecutive_exploratory
    all_exploratory = all(tc.name in _EXPLORATORY_TOOLS for tc in tool_calls)
    return consecutive_exploratory + 1 if all_exploratory else 0


def _count_business_artifacts(artifact_store: Any) -> int:
    """Count non-debug artifacts in the store."""
    if artifact_store is None:
        return 0
    try:
        return len([
            a for a in artifact_store.list_all()
            if not a.metadata.debug_only
        ])
    except Exception:
        return 0
```

- [ ] **Step 2: Update loop.py**

Remove from `loop.py`:
- Constants `_MAX_REASONING_DUPE_STEPS` through `_MAX_CONSECUTIVE_EXPLORATORY` (lines 28-44)
- `_EXPLORATORY_TOOLS` (line 44)
- `_MAX_TURNS_WITHOUT_BUSINESS_ARTIFACTS` (line 48)
- `_check_explore_loop` (lines 604-662)
- `_update_null_tracking` (lines 665-677)
- `_normalize_args_for_dedup` (lines 679-687) — already removed in Task 2.1
- `_update_tool_call_history` (lines 690-700)
- `_count_business_artifacts` (lines 703-713)
- `_update_exploratory_tracking` (lines 716-728)

Add import at top:
```python
from .loop_guards import (
    check_explore_loop,
    detect_reasoning_loop,
    check_business_artifact_progress,
    update_null_tracking,
    update_tool_call_history,
    update_exploratory_tracking,
)
```

Update call sites in `agent_loop()`:
- `_check_explore_loop(...)` → `check_explore_loop(...)` (line 467)
- `_update_null_tracking(results, recent_null_results)` → `update_null_tracking(results, recent_null_results)` (lines 473, 491)
- `_update_tool_call_history(...)` → `update_tool_call_history(...)` (lines 474, 492)
- `_count_business_artifacts(artifact_store)` → from `loop_guards` — used inside `check_business_artifact_progress` now
- `_update_exploratory_tracking(...)` → `update_exploratory_tracking(...)` (lines 476, 493)
- Reasoning loop detection block (lines 229-253) → use `detect_reasoning_loop(recent_reasoning)`
- Business artifact progress block (lines 353-385) → use `check_business_artifact_progress(artifact_store, initial_business_count, turns_since_last_business_artifact)`

- [ ] **Step 3: Run tests**

Run: `uv run pytest tests/agent/test_loop.py -v --timeout=30 2>&1 | tail -20`
Expected: All tests pass

- [ ] **Step 4: Commit**

```bash
git add src/agent/core/loop_guards.py src/agent/core/loop.py
git commit -m "refactor: extract loop_guards.py from loop.py"
```

---

### Task 2.3: Extract loop_hints.py — artifact readiness and blocked-tool hints

**Files:**
- Create: `src/agent/core/loop_hints.py`
- Modify: `src/agent/core/loop.py`

- [ ] **Step 1: Create loop_hints.py**

Create `src/agent/core/loop_hints.py`:

```python
"""Artifact readiness hints — inject system messages when tools are ready or blocked."""

from __future__ import annotations

import logging
from typing import Any

from ..artifacts.resolver import emit_event

logger = logging.getLogger(__name__)

# Max exploratory calls while a terminal tool is ready.
_MAX_EXPLORATORY_WHEN_TERMINAL_READY = 2


def check_and_inject_hints(
    *,
    tool_registry: Any | None,
    artifact_store: Any | None,
    consecutive_exploratory: int,
    current_state: Any,  # AgentState
) -> Any:
    """Unified entry point for terminal-tool readiness and blocked-tool hints.

    Returns (possibly modified) AgentState.
    """
    terminal_ready = _is_terminal_tool_ready(tool_registry, artifact_store)

    if terminal_ready:
        return _handle_terminal_ready(
            tool_registry=tool_registry,
            artifact_store=artifact_store,
            consecutive_exploratory=consecutive_exploratory,
            current_state=current_state,
        )

    if consecutive_exploratory >= 2:
        return _handle_blocked_tools(
            tool_registry=tool_registry,
            artifact_store=artifact_store,
            current_state=current_state,
        )

    return current_state


def _handle_terminal_ready(
    *,
    tool_registry: Any,
    artifact_store: Any,
    consecutive_exploratory: int,
    current_state: Any,
) -> Any:
    """Inject hints when terminal tools are ready; escalate to blocking if ignored."""
    ready_tools = _get_ready_terminal_tools(tool_registry, artifact_store)
    emit_event("terminal_tool_ready", {"tools": ready_tools})

    if consecutive_exploratory == 0:
        tool_hints = _build_terminal_ready_hints(tool_registry, artifact_store)
        if tool_hints:
            hint_msg = (
                "\n\n[系统提示] 以下业务工具所需参数已自动准备就绪，"
                "可直接调用，无需再通过 get_artifact 获取数据：\n"
                + tool_hints
            )
            return _append_system_hint(current_state, hint_msg)

    elif consecutive_exploratory >= _MAX_EXPLORATORY_WHEN_TERMINAL_READY:
        hint_msg = (
            "\n\n[系统提示] 业务工具（如 detect_plagiarism）所需的参数"
            "已自动准备就绪。请直接调用目标业务工具，"
            "无需再调用 get_artifact 或 list_artifacts。"
        )
        new_state = _append_system_hint(current_state, hint_msg)

        if consecutive_exploratory >= _MAX_EXPLORATORY_WHEN_TERMINAL_READY * 2:
            return new_state.model_copy(
                update={
                    "status": "completed",
                    "termination_reason": "terminal_tool_ready_but_ignored",
                }
            )
        return new_state

    return current_state


def _handle_blocked_tools(
    *,
    tool_registry: Any,
    artifact_store: Any,
    current_state: Any,
) -> Any:
    """Inject hints about which tools are blocked and why."""
    blocked_hints = _build_blocked_tools_hints(tool_registry, artifact_store)
    if blocked_hints:
        hint_msg = (
            "\n\n[系统提示] 以下业务工具因缺少必要输入暂无法调用：\n"
            + blocked_hints
            + "\n请先调用建议的上游工具获取所需数据。"
        )
        return _append_system_hint(current_state, hint_msg)
    return current_state


def _append_system_hint(state: Any, hint_msg: str) -> Any:
    """Append a system hint message to the agent state."""
    from .state import Message
    new_messages = list(state.messages)
    new_messages.append(Message(role="user", content=hint_msg))
    return state.model_copy(update={"messages": tuple(new_messages)})


def _is_terminal_tool_ready(tool_registry: Any | None, artifact_store: Any | None) -> bool:
    """Check whether any tool with require_contract_binding=True is ready."""
    if tool_registry is None or artifact_store is None:
        return False
    try:
        from src.agent.artifacts.models import ProjectionPolicy
        from src.agent.artifacts.projectors import create_default_projector_registry
        from src.agent.artifacts.resolver import ProjectionResolver
    except ImportError:
        return False
    for tool in tool_registry.list_tools():
        input_contract = getattr(tool, "input_contract", None)
        if input_contract is None or not input_contract.require_contract_binding:
            continue
        candidates = artifact_store.list_projection_candidates()
        if not candidates:
            continue
        resolver = ProjectionResolver(create_default_projector_registry())
        resolution = resolver.resolve(input_contract, candidates, ProjectionPolicy())
        if resolution.status == "resolved":
            return True
    return False


def _get_ready_terminal_tools(tool_registry: Any | None, artifact_store: Any | None) -> list[str]:
    """Return the list of terminal tool names that are ready to be called."""
    ready: list[str] = []
    if tool_registry is None or artifact_store is None:
        return ready
    try:
        from src.agent.artifacts.models import ProjectionPolicy
        from src.agent.artifacts.projectors import create_default_projector_registry
        from src.agent.artifacts.resolver import ProjectionResolver
    except ImportError:
        return ready
    for tool in tool_registry.list_tools():
        input_contract = getattr(tool, "input_contract", None)
        if input_contract is None or not input_contract.require_contract_binding:
            continue
        candidates = artifact_store.list_projection_candidates()
        if not candidates:
            continue
        resolver = ProjectionResolver(create_default_projector_registry())
        resolution = resolver.resolve(input_contract, candidates, ProjectionPolicy())
        if resolution.status == "resolved":
            ready.append(input_contract.tool_name)
    return ready


def _build_terminal_ready_hints(tool_registry: Any | None, artifact_store: Any | None) -> str | None:
    """Build a human-readable summary of ready terminal tools."""
    if tool_registry is None or artifact_store is None:
        return None
    try:
        from src.agent.artifacts.models import ProjectionPolicy
        from src.agent.artifacts.projectors import create_default_projector_registry
        from src.agent.artifacts.resolver import ProjectionResolver
    except ImportError:
        return None
    lines: list[str] = []
    for tool in tool_registry.list_tools():
        input_contract = getattr(tool, "input_contract", None)
        if input_contract is None or not input_contract.require_contract_binding:
            continue
        candidates = artifact_store.list_projection_candidates()
        if not candidates:
            continue
        resolver = ProjectionResolver(create_default_projector_registry())
        resolution = resolver.resolve(input_contract, candidates, ProjectionPolicy())
        if resolution.status == "resolved":
            emit_event("tool_ready", {
                "tool": input_contract.tool_name,
                "fields": list(resolution.plans.keys()),
            })
            lines.append(f"- {input_contract.tool_name} 可以立即调用。输入将自动绑定：")
            for field_name, plan in resolution.plans.items():
                source = plan.source_artifact_id
                steps_desc = (
                    "（直接匹配）" if not plan.steps
                    else f"（通过 {len(plan.steps)} 步投影）"
                )
                lines.append(f"  {field_name} ← {source} {steps_desc}")
    return "\n".join(lines) if lines else None


def _build_blocked_tools_hints(tool_registry: Any | None, artifact_store: Any | None) -> str | None:
    """Build hints for tools that are blocked due to missing artifacts."""
    if tool_registry is None or artifact_store is None:
        return None
    try:
        from src.agent.artifacts.models import ProjectionPolicy
        from src.agent.artifacts.projectors import create_default_projector_registry
        from src.agent.artifacts.resolver import ProjectionResolver
    except ImportError:
        return None
    lines: list[str] = []
    for tool in tool_registry.list_tools():
        input_contract = getattr(tool, "input_contract", None)
        if input_contract is None or not input_contract.require_contract_binding:
            continue
        candidates = artifact_store.list_projection_candidates()
        resolver = ProjectionResolver(create_default_projector_registry())
        resolution = resolver.resolve(input_contract, candidates, ProjectionPolicy())
        if resolution.status == "failed":
            lines.append(f"- {input_contract.tool_name} 无法调用（缺少必要输入）：")
            for diagnostic in resolution.diagnostics:
                lines.append(f"  {diagnostic.message}")
            for action in resolution.suggested_actions:
                action_tool = action.get("tool", "")
                reason = action.get("reason", "")
                lines.append(f"  建议：调用 {action_tool} — {reason}")
                emit_event("tool_blocked_missing_artifact", {
                    "tool": input_contract.tool_name,
                    "missing_field": action.get("field", ""),
                    "suggested_tool": action_tool,
                })
    return "\n".join(lines) if lines else None
```

- [ ] **Step 2: Update loop.py**

Remove from `loop.py` (lines 731-858):
- `_build_blocked_tools_hints`
- `_is_terminal_tool_ready`
- `_get_ready_terminal_tools`
- `_build_terminal_ready_hints`

Remove constants (lines 39, 41):
- `_MAX_EXPLORATORY_WHEN_TERMINAL_READY`

Add import at top:
```python
from .loop_hints import check_and_inject_hints
```

Replace the terminal-tool readiness block (lines 387-464) with:
```python
        # -- Terminal-tool readiness / blocked-tools hints --
        current_state = check_and_inject_hints(
            tool_registry=tool_registry,
            artifact_store=artifact_store,
            consecutive_exploratory=consecutive_exploratory,
            current_state=current_state,
        )
        if current_state.is_terminal():
            if on_step:
                await on_step("loop", current_state.termination_reason or "hints_terminated")
            if audit_logger:
                write_audit_turn(
                    audit_logger, turn_index, timestamp,
                    llm_request, llm_response, tuple(tool_records),
                )
            break
```

- [ ] **Step 3: Run tests**

Run: `uv run pytest tests/agent/test_loop.py -v --timeout=30 2>&1 | tail -20`
Expected: All tests pass

- [ ] **Step 4: Commit**

```bash
git add src/agent/core/loop_hints.py src/agent/core/loop.py
git commit -m "refactor: extract loop_hints.py from loop.py"
```

---

### Task 2.4: Slim down loop.py — verify final line count

**Files:**
- Verify: `src/agent/core/loop.py`

- [ ] **Step 1: Check line count**

Run: `wc -l src/agent/core/loop.py`
Expected: < 300 lines

- [ ] **Step 2: Run full agent test suite**

Run: `uv run pytest tests/agent/ -v --timeout=60 2>&1 | tail -30`
Expected: All tests pass

- [ ] **Step 3: Commit (if any final cleanup needed)**

```bash
git add src/agent/core/loop.py
git commit -m "refactor: final cleanup of slimmed loop.py"
```

---

### Task 3.1: Create scanned/ subpackage — preprocessor + OCR

**Files:**
- Create: `src/docparse/parsers/scanned/__init__.py`
- Create: `src/docparse/parsers/scanned/preprocessor.py`
- Create: `src/docparse/parsers/scanned/ocr_engine.py`
- Modify: `src/docparse/parsers/scanned_parser.py`

- [ ] **Step 1: Create scanned/__init__.py**

```python
"""Scanned document parser — seven-stage pipeline for image/PDF-to-Document.

Stages: preprocess → OCR → spacing → font → normalize → LLM → structure.
"""

from .preprocessor import prepare_images, pdf_to_images, resize_images_for_ocr, cleanup_temp_images
from .ocr_engine import parallel_ocr, ocr_result_to_lines
from .spacing import merge_spacing_into_page_content, normalize_body_line_spacing, adjust_cross_page_spacing
from .font_detector import merge_font_info, refine_font_size_by_chars_per_line
from .normalizer import normalize_coordinates, classify_body_text_heuristic
from .llm_completer import complete_structure_with_llm
from .structure import build_document_structure, build_block_outline_map_from_blocks, get_outline_for_line

__all__ = [
    "prepare_images",
    "pdf_to_images",
    "resize_images_for_ocr",
    "cleanup_temp_images",
    "parallel_ocr",
    "ocr_result_to_lines",
    "merge_spacing_into_page_content",
    "normalize_body_line_spacing",
    "adjust_cross_page_spacing",
    "merge_font_info",
    "refine_font_size_by_chars_per_line",
    "normalize_coordinates",
    "classify_body_text_heuristic",
    "complete_structure_with_llm",
    "build_document_structure",
    "build_block_outline_map_from_blocks",
    "get_outline_for_line",
]
```

- [ ] **Step 2: Move image preprocessing functions to preprocessor.py**

Extract from `scanned_parser.py` (lines 892-1003): `_prepare_images`, `_pdf_to_images`, `_resize_images_for_ocr`, `_cleanup_temp_images`
Rename to public names: `prepare_images`, `pdf_to_images`, `resize_images_for_ocr`, `cleanup_temp_images`

- [ ] **Step 3: Move OCR functions to ocr_engine.py**

Extract from `scanned_parser.py` (lines 278-387): `_parallel_ocr`, `_ocr_result_to_lines`
Rename to public names: `parallel_ocr`, `ocr_result_to_lines`

- [ ] **Step 4: Update scanned_parser.py imports**

Replace extracted code with:
```python
from .scanned.preprocessor import prepare_images, pdf_to_images, resize_images_for_ocr, cleanup_temp_images
from .scanned.ocr_engine import parallel_ocr, ocr_result_to_lines
```

Update `ScannedParser.parse()` call sites from `_prepare_images` → `prepare_images`, etc.

- [ ] **Step 5: Run scanned parser tests**

Run: `uv run pytest src/docparse/tests/test_scanned_parser.py -v --timeout=60 2>&1 | tail -10`
Expected: Tests pass (or skip if OCR not available)

- [ ] **Step 6: Commit**

```bash
git add src/docparse/parsers/scanned/ src/docparse/parsers/scanned_parser.py
git commit -m "refactor: extract preprocessor and OCR modules from scanned_parser.py"
```

---

### Task 3.2: Complete scanned/ subpackage — spacing, font, normalizer, LLM, structure

**Files:**
- Create: `src/docparse/parsers/scanned/spacing.py`
- Create: `src/docparse/parsers/scanned/font_detector.py`
- Create: `src/docparse/parsers/scanned/normalizer.py`
- Create: `src/docparse/parsers/scanned/llm_completer.py`
- Create: `src/docparse/parsers/scanned/structure.py`
- Modify: `src/docparse/parsers/scanned_parser.py`

- [ ] **Step 1: Move spacing functions**

Extract from `scanned_parser.py` (lines 467-860):
`_merge_spacing_into_page_content`, `_normalize_body_line_spacing`, `_histogram_cluster_pt`, `_adjust_cross_page_spacing`, `_is_cross_page_continuation`
→ `src/docparse/parsers/scanned/spacing.py` as public functions

- [ ] **Step 2: Move font detection functions**

Extract from `scanned_parser.py` (lines 441-664):
`_merge_font_info`, `_refine_font_size_by_chars_per_line`
→ `src/docparse/parsers/scanned/font_detector.py`

- [ ] **Step 3: Move normalization functions**

Extract from `scanned_parser.py` (lines 389-428):
`_classify_body_text_heuristic`, coordinate normalization logic
→ `src/docparse/parsers/scanned/normalizer.py`

- [ ] **Step 4: Move LLM completion**

Extract LLM structure completion logic
→ `src/docparse/parsers/scanned/llm_completer.py`

- [ ] **Step 5: Move structure functions**

Extract from `scanned_parser.py` (lines 411-437, 504-564):
`_build_block_outline_map_from_blocks`, `_get_outline_for_line`, `_collect_all_paragraphs`
→ `src/docparse/parsers/scanned/structure.py`

- [ ] **Step 6: Update scanned_parser.py**

Replace extracted code with imports from the new modules. `ScannedParser` class stays in `scanned_parser.py` as a thin orchestrator.

- [ ] **Step 7: Run tests**

Run: `uv run pytest src/docparse/tests/ -v --timeout=60 2>&1 | tail -15`
Expected: All tests pass

- [ ] **Step 8: Commit**

```bash
git add src/docparse/parsers/scanned/ src/docparse/parsers/scanned_parser.py
git commit -m "refactor: complete scanned/ subpackage extraction from scanned_parser.py"
```

---

### Task 3.3: Delete scanned_parser.py, use scanned/ as canonical

**Files:**
- Modify: `src/docparse/__init__.py`
- Modify: `src/docparse/parsers/__init__.py`
- Delete: `src/docparse/parsers/scanned_parser.py`

- [ ] **Step 1: Move ScannedParser class into scanned/__init__.py**

Add the `ScannedParser` class (the thin orchestrator) into `src/docparse/parsers/scanned/__init__.py`.

- [ ] **Step 2: Update all imports**

Update `src/docparse/__init__.py`:
```python
# Change:
# from docparse.parsers.scanned_parser import ScannedParser
# To:
from docparse.parsers.scanned import ScannedParser
```

Update any other files importing from `scanned_parser`:
Run: `grep -r "scanned_parser" src/ tests/ --include="*.py" -l`
For each file found, update the import path.

- [ ] **Step 3: Delete old file and run tests**

```bash
rm src/docparse/parsers/scanned_parser.py
uv run pytest src/docparse/tests/ -v --timeout=60 2>&1 | tail -15
```
Expected: All tests pass

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "refactor: replace scanned_parser.py with scanned/ subpackage"
```

---

### Task 3.4: Split template_service.py → templates/ subpackage

**Files:**
- Create: `src/validator/templates/__init__.py`
- Create: `src/validator/templates/crud.py`
- Create: `src/validator/templates/skeleton.py`
- Create: `src/validator/templates/preview.py`
- Modify: `src/validator/__init__.py`
- Delete: `src/validator/template_service.py`

- [ ] **Step 1: Create templates/ subpackage structure**

**crud.py** — `load_template_from_db`, `to_legacy_format`, `to_canvas_format`, `canvas_to_new_format`
**skeleton.py** — `load_builtin_skeleton`, `_merge_variant`, `_block_to_paragraph`
**preview.py** — `spec_from_template_content`, `spec_to_sample_document`, `get_default_seal_base64`, `replace_default_seal`

- [ ] **Step 2: Create templates/__init__.py with re-exports**

```python
"""Format template management — CRUD, skeleton generation, preview rendering."""

from .crud import load_template_from_db, to_legacy_format, to_canvas_format, canvas_to_new_format
from .skeleton import load_builtin_skeleton
from .preview import spec_from_template_content, spec_to_sample_document, get_default_seal_base64, replace_default_seal

__all__ = [
    "load_template_from_db",
    "to_legacy_format",
    "to_canvas_format",
    "canvas_to_new_format",
    "load_builtin_skeleton",
    "spec_from_template_content",
    "spec_to_sample_document",
    "get_default_seal_base64",
    "replace_default_seal",
]
```

- [ ] **Step 3: Update validator/__init__.py and delete old file**

Update import in `src/validator/__init__.py` (if template_service is imported there).
Search for all imports of `template_service`:
Run: `grep -r "template_service" src/ tests/ --include="*.py" -l`
Update each file.

Delete `src/validator/template_service.py`.

- [ ] **Step 4: Run tests**

Run: `uv run pytest src/validator/tests/ -v --timeout=30 2>&1 | tail -10`
Expected: All tests pass

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor: split template_service.py into templates/ subpackage"
```

---

### Task 3.5: Split subagent.py → subagent/ subpackage

**Files:**
- Create: `src/agent/agents/subagent/__init__.py`
- Create: `src/agent/agents/subagent/config.py`
- Create: `src/agent/agents/subagent/runner.py`
- Create: `src/agent/agents/subagent/tool.py`
- Modify: `src/agent/agents/orch.py` (update import)
- Delete: `src/agent/agents/subagent.py`

- [ ] **Step 1: Create subagent/ subpackage structure**

**config.py** — `SubAgentConfig`, `FailureStrategy`, `_CallbackHolder`, `_annotation_expects_type`
**runner.py** — `SubAgentRunner` class (dispatch, lifecycle, scoped stores)
**tool.py** — `_SubAgentTool` class, `_extract_result_data`, `_describe_failure`, `_with_call_metadata`, `_REF_PATTERN`

- [ ] **Step 2: Create subagent/__init__.py with re-exports**

```python
"""Subagent system — lifecycle, dispatch, structured input/output."""

from .config import SubAgentConfig, FailureStrategy
from .runner import SubAgentRunner
from .tool import _SubAgentTool

__all__ = [
    "SubAgentConfig",
    "FailureStrategy",
    "SubAgentRunner",
    "_SubAgentTool",
]
```

- [ ] **Step 3: Update all imports**

Run: `grep -r "from .subagent import\|from ..agents.subagent import" src/ tests/ --include="*.py" -l`

Update each file:
```python
# Change:
# from .subagent import SubAgentRunner, SubAgentConfig, FailureStrategy
# To:
from .subagent import SubAgentRunner, SubAgentConfig, FailureStrategy
```

The `from .subagent import ...` style should still work since we're replacing the module with a package — Python will find `subagent/__init__.py`.

- [ ] **Step 4: Delete old file and run tests**

```bash
rm src/agent/agents/subagent.py
uv run pytest tests/agent/test_subagent.py tests/agent/test_subagent_coerce.py -v --timeout=30 2>&1 | tail -15
```
Expected: All tests pass

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor: split subagent.py into subagent/ subpackage"
```

---

### Task 4.1: Move content_compliance tests + remove auto-register

**Files:**
- Create: `tests/content_compliance/__init__.py`
- Move: `src/content_compliance/tests/*` → `tests/content_compliance/`
- Modify: `src/content_compliance/__init__.py`

- [ ] **Step 1: Move test files**

```bash
mkdir -p tests/content_compliance
cp src/content_compliance/tests/*.py tests/content_compliance/
# Verify tests still run from new location
uv run pytest tests/content_compliance/ -v --timeout=30 2>&1 | tail -10
```

- [ ] **Step 2: Fix imports in moved tests**

The test files may have relative imports that need updating. Check each:
Run: `grep -r "from \.\|from src.content_compliance" tests/content_compliance/ --include="*.py"`

Update any broken imports. Typically tests use `from src.content_compliance.xxx import yyy` which should still work.

- [ ] **Step 3: Replace auto-register with explicit init_checkers()**

In `src/content_compliance/__init__.py`, replace:
```python
_auto_register()
```
With:
```python
def init_checkers(rules_base: Path | None = None) -> None:
    """Explicitly register all content compliance checkers.

    Args:
        rules_base: Path to rules directory. Uses default if None.
    """
    base = rules_base or _RULES_BASE
    if not base.exists():
        logger.warning("Rules directory %s not found, no checkers registered", base)
        return
    for rules_file in base.glob("*.json"):
        try:
            doc_type, subtypes = load_checker_config(rules_file)
            checker = RuleBasedContentChecker(doc_type, subtypes)
            register(checker)
        except Exception as e:
            logger.error("Failed to load rules from %s: %s", rules_file, e)
```

Remove the module-level `_auto_register()` call.

- [ ] **Step 4: Add init_checkers() call to app startup**

In `src/agent/api/app.py` `create_app()`, after settings initialization:
```python
from src.content_compliance import init_checkers
init_checkers()
```

- [ ] **Step 5: Remove old test directory**

```bash
rm -rf src/content_compliance/tests/
```

- [ ] **Step 6: Run all tests**

Run: `uv run pytest tests/content_compliance/ -v --timeout=30 2>&1 | tail -10`
Expected: All tests pass

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "refactor: move content_compliance tests, replace auto-register with init_checkers()"
```

---

### Task 4.2: Update pyproject.toml

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Remove content_compliance test exclusion**

In `pyproject.toml`, remove the line:
```toml
exclude = ["src/content_compliance/tests"]
```

The `[tool.hatch.build]` section should just be:
```toml
[tool.hatch.build]
sources = ["src"]
```

- [ ] **Step 2: Verify build**

Run: `uv run python -c "import src.content_compliance; print('Import OK')"`
Expected: prints "Import OK"

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml
git commit -m "build: remove content_compliance test exclusion from hatch config"
```

---

### Task 5.1: Create domain tool adapters (part 1 — parse, format, content)

**Files:**
- Create: `src/agent/tools/domain/__init__.py`
- Create: `src/agent/tools/domain/parse_tool.py`
- Create: `src/agent/tools/domain/format_check_tool.py`
- Create: `src/agent/tools/domain/content_compliance_tool.py`

- [ ] **Step 1: Create domain/__init__.py**

```python
"""Domain tool adapters — wrap docparse, validator, content_compliance, etc. as ToolProtocol."""

from .parse_tool import ParseDocumentTool
from .format_check_tool import FormatCheckTool
from .content_compliance_tool import ContentComplianceTool
from .text_correction_tool import TextCorrectionTool
from .plagiarism_tool import PlagiarismCheckTool
from .annotate_tool import AnnotateTool

DOMAIN_TOOLS = [
    ParseDocumentTool(),
    FormatCheckTool(),
    ContentComplianceTool(),
    TextCorrectionTool(),
    PlagiarismCheckTool(),
    AnnotateTool(),
]

__all__ = [
    "DOMAIN_TOOLS",
    "ParseDocumentTool",
    "FormatCheckTool",
    "ContentComplianceTool",
    "TextCorrectionTool",
    "PlagiarismCheckTool",
    "AnnotateTool",
]
```

- [ ] **Step 2: Create parse_tool.py**

```python
"""Tool adapter for docparse — parse documents into structured models."""

from __future__ import annotations

from typing import Any

from ...tools.protocol import ToolProtocol, ToolResult


class ParseDocumentTool:
    """Parse a document file (PDF/DOCX/image) into structured data."""

    name = "parse_document_direct"
    description = (
        "直接解析文档文件（PDF/DOCX/图片）为结构化 Document 模型。"
        "返回 pages 数组，每页包含 header/body/footer 结构。"
        "不需要 LLM Agent 编排，直接调用底层解析器。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "文档文件的绝对路径",
            },
        },
        "required": ["file_path"],
    }

    async def execute(self, file_path: str, **kwargs: Any) -> ToolResult:
        """Parse document and return structured result."""
        import os
        from src.docparse import parse_pdf, parse_docx, parse_scanned

        ext = os.path.splitext(file_path)[1].lower()

        try:
            if ext == ".pdf":
                doc = parse_pdf(file_path)
            elif ext == ".docx":
                doc = parse_docx(file_path)
            elif ext in (".bmp", ".jpg", ".jpeg", ".png", ".gif", ".tif", ".tiff"):
                doc = parse_scanned(file_path)
            else:
                return ToolResult(
                    success=False,
                    error=f"不支持的文件格式: {ext}",
                )
            return ToolResult(
                success=True,
                data=doc.model_dump(),
            )
        except Exception as exc:
            return ToolResult(
                success=False,
                error=f"文档解析失败: {exc}",
            )
```

- [ ] **Step 3: Create format_check_tool.py**

```python
"""Tool adapter for validator — document format checking."""

from __future__ import annotations

from typing import Any

from ...tools.protocol import ToolProtocol, ToolResult


class FormatCheckTool:
    """Format validation tool for government documents."""

    name = "format_check_direct"
    description = (
        "直接校验公文格式是否符合 GB/T 9704-2012 标准。"
        "比对文档与标准模板的字体、字号、位置、对齐等要素。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "文档文件路径",
            },
            "doc_type": {
                "type": "string",
                "description": "文档类型（通知/函/请示/报告/批复），可选，自动检测",
            },
        },
        "required": ["file_path"],
    }

    async def execute(self, file_path: str, doc_type: str = "", **kwargs: Any) -> ToolResult:
        """Run format validation."""
        try:
            from src.validator import compare_document_to_spec, detect_wenzhong_from_doc

            if not doc_type:
                wenzhong = detect_wenzhong_from_doc(file_path)
                doc_type = wenzhong or "通知"

            result = compare_document_to_spec(file_path, doc_type)
            return ToolResult(
                success=True,
                data={"doc_type": doc_type, **result} if isinstance(result, dict) else result,
            )
        except Exception as exc:
            return ToolResult(
                success=False,
                error=f"格式校验失败: {exc}",
            )
```

- [ ] **Step 4: Create content_compliance_tool.py**

```python
"""Tool adapter for content_compliance — content rules checking."""

from __future__ import annotations

from typing import Any

from ...tools.protocol import ToolProtocol, ToolResult


class ContentComplianceTool:
    """Content compliance checker for government documents."""

    name = "content_compliance_direct"
    description = (
        "直接校验公文内容合规性。按文档子类型匹配对应的内容规则进行检查。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "document_data": {
                "type": "object",
                "description": "已解析的文档数据（Document model dump）",
            },
            "doc_type": {
                "type": "string",
                "description": "文档子类型（如：指示性通知、请示、报告）",
            },
        },
        "required": ["document_data", "doc_type"],
    }

    async def execute(self, document_data: dict, doc_type: str, **kwargs: Any) -> ToolResult:
        """Run content compliance check."""
        try:
            from src.content_compliance import get_checker

            checker = get_checker(doc_type)
            if checker is None:
                return ToolResult(
                    success=False,
                    error=f"未找到文档类型 '{doc_type}' 的内容检查器",
                )
            result = checker.check(document_data)
            return ToolResult(
                success=True,
                data=result.model_dump() if hasattr(result, "model_dump") else result,
            )
        except Exception as exc:
            return ToolResult(
                success=False,
                error=f"内容合规检查失败: {exc}",
            )
```

- [ ] **Step 5: Verify imports**

Run: `uv run python -c "from src.agent.tools.domain import ParseDocumentTool, FormatCheckTool, ContentComplianceTool; print('Domain tools OK')"`
Expected: prints "Domain tools OK"

- [ ] **Step 6: Commit**

```bash
git add src/agent/tools/domain/
git commit -m "feat: add domain tool adapters — parse, format, content"
```

---

### Task 5.2: Create domain tool adapters (part 2 — correction, plagiarism, annotate)

**Files:**
- Create: `src/agent/tools/domain/text_correction_tool.py`
- Create: `src/agent/tools/domain/plagiarism_tool.py`
- Create: `src/agent/tools/domain/annotate_tool.py`

- [ ] **Step 1: Create text_correction_tool.py**

```python
"""Tool adapter for doccorrector — Chinese text correction."""

from __future__ import annotations

from typing import Any

from ...tools.protocol import ToolProtocol, ToolResult


class TextCorrectionTool:
    """Chinese text correction tool."""

    name = "text_correction_direct"
    description = (
        "直接对中文文本进行纠错。六阶段流水线：用户词典 → 纠错模型 → 规则检测 → 双层冲突消解。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "需要纠错的中文文本",
            },
        },
        "required": ["text"],
    }

    async def execute(self, text: str, **kwargs: Any) -> ToolResult:
        """Run text correction."""
        try:
            from src.doccorrector.corrector import correct_text

            result = correct_text(text)
            return ToolResult(
                success=True,
                data=result if isinstance(result, dict) else {"corrected_text": str(result)},
            )
        except Exception as exc:
            return ToolResult(
                success=False,
                error=f"文本纠错失败: {exc}",
            )
```

- [ ] **Step 2: Create plagiarism_tool.py**

```python
"""Tool adapter for dedump — document plagiarism detection."""

from __future__ import annotations

from typing import Any

from ...tools.protocol import ToolProtocol, ToolResult


class PlagiarismCheckTool:
    """Document plagiarism detection tool."""

    name = "plagiarism_check_direct"
    description = (
        "直接进行文档查重检测。段落级相似度比对，Tukey IQR 动态阈值，双门控机制。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "document_text": {
                "type": "string",
                "description": "待检测的文档文本",
            },
        },
        "required": ["document_text"],
    }

    async def execute(self, document_text: str, **kwargs: Any) -> ToolResult:
        """Run plagiarism check."""
        try:
            from src.dedump.dedump import check_plagiarism

            result = check_plagiarism(document_text)
            return ToolResult(
                success=True,
                data=result if isinstance(result, dict) else {"result": str(result)},
            )
        except Exception as exc:
            return ToolResult(
                success=False,
                error=f"查重检测失败: {exc}",
            )
```

- [ ] **Step 3: Create annotate_tool.py**

```python
"""Tool adapter for docannot — annotation export."""

from __future__ import annotations

from typing import Any

from ...tools.protocol import ToolProtocol, ToolResult


class AnnotateTool:
    """Document annotation export tool."""

    name = "annotate_direct"
    description = (
        "直接对文档进行批注导出。将审核发现标注到 DOCX 或 PDF 文件中。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "原始文档路径",
            },
            "rules": {
                "type": "array",
                "description": "批注规则列表",
                "items": {"type": "object"},
            },
        },
        "required": ["file_path", "rules"],
    }

    async def execute(self, file_path: str, rules: list, **kwargs: Any) -> ToolResult:
        """Export annotations to document."""
        try:
            from src.docannot import annotate, Rule

            annotate_rules = [Rule(**r) if isinstance(r, dict) else r for r in rules]
            output_path = annotate(file_path, annotate_rules)
            return ToolResult(
                success=True,
                data={"output_path": str(output_path)},
            )
        except Exception as exc:
            return ToolResult(
                success=False,
                error=f"批注导出失败: {exc}",
            )
```

- [ ] **Step 4: Verify all domain tools import correctly**

Run: `uv run python -c "from src.agent.tools.domain import DOMAIN_TOOLS; print(f'{len(DOMAIN_TOOLS)} domain tools loaded')"`
Expected: prints "6 domain tools loaded"

- [ ] **Step 5: Commit**

```bash
git add src/agent/tools/domain/
git commit -m "feat: add domain tool adapters — correction, plagiarism, annotate"
```

---

### Task 5.3: Register domain tools at app startup

**Files:**
- Modify: `src/agent/api/app.py`

- [ ] **Step 1: Register domain tools in create_app()**

In `src/agent/api/app.py`, after creating `app.state.tool_registry`, add domain tool registration:

```python
    # Register domain tools so both Agent and direct-call paths share them
    from ..tools.domain import DOMAIN_TOOLS
    for tool in DOMAIN_TOOLS:
        app.state.tool_registry.register(tool)
```

- [ ] **Step 2: Verify registration**

Run: `uv run python -c "
from src.agent.api.app import create_app
app = create_app()
tools = app.state.tool_registry.list_tools()
print(f'Registered {len(tools)} tools:', [t.name for t in tools])
"`
Expected: prints list of registered tools including the 6 domain tools

- [ ] **Step 3: Commit**

```bash
git add src/agent/api/app.py
git commit -m "feat: register domain tools in app startup ToolRegistry"
```

---

### Task 5.4: Clean up agent subagent duplication with domain tools

**Files:**
- Modify: `src/agent/agents/orch.py`
- Modify: `src/agent/agents/base.py` (AuditorAgent)

- [ ] **Step 1: Audit which subagent tools overlap with domain tools**

The `OrchestratorAgent` configures `_SUBAGENTS` — each wraps a domain auditor (FormatAuditorAgent, ContentAuditorAgent, etc.). These are already agent-based wrappers with LLM prompts.

The domain tools (`format_check_direct`, `content_compliance_direct`) are direct, non-LLM wrappers.

These serve different purposes and do NOT duplicate — the agent versions include LLM reasoning. Keep both.

- [ ] **Step 2: Add domain tools to OrchestratorAgent's tool list**

In `src/agent/agents/orch.py`, modify the `__init__` method to also register domain tools alongside subagent tools:

```python
        # Build tools from subagents, plus parent-level artifact tools and domain tools.
        from ..tools.domain import DOMAIN_TOOLS

        tools = self._subagent_runner.build_tools()
        tools.append(ListArtifactsTool())
        tools.append(GetArtifactTool())
        tools.append(PersistOutputTool())
        tools.extend(DOMAIN_TOOLS)  # <-- add domain tools
```

- [ ] **Step 3: Run orchestrator tests**

Run: `uv run pytest tests/agent/test_orchestrator.py -v --timeout=30 2>&1 | tail -10`
Expected: Tests pass

- [ ] **Step 4: Commit**

```bash
git add src/agent/agents/orch.py
git commit -m "feat: add domain tools to OrchestratorAgent tool list"
```

---

### Task 6.1: Extract agent_service.py

**Files:**
- Create: `src/agent/api/services/__init__.py`
- Create: `src/agent/api/services/agent_service.py`

- [ ] **Step 1: Create services/__init__.py**

```python
"""Service layer — async functions that bridge HTTP routes and agent/domain logic."""

from .agent_service import build_audit_agent, build_chat_agent
from .session_service import list_sessions, get_session, create_session, delete_session
from .file_service import upload_file
from .stream_service import generate_sse_stream

__all__ = [
    "build_audit_agent",
    "build_chat_agent",
    "list_sessions",
    "get_session",
    "create_session",
    "delete_session",
    "upload_file",
    "generate_sse_stream",
]
```

- [ ] **Step 2: Create agent_service.py**

Copy the `_build_model_client`, `_build_api_agent`, and `_build_chat_agent` functions from `routes.py` (lines 34-90) into `agent_service.py`, making them public and accepting dependencies as parameters:

```python
"""Agent construction services."""

from __future__ import annotations

from src.config import Settings
from src.agent.core.model import OpenAIModelClient
from src.agent.core.context_manager import ContextManager


def build_model_client(settings: Settings) -> OpenAIModelClient:
    """Create an OpenAIModelClient from Settings."""
    return OpenAIModelClient(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        model=settings.llm_model,
        temperature=settings.llm_temperature,
        max_tokens=settings.llm_max_tokens if settings.llm_max_tokens > 0 else None,
        extra_body=settings.llm_extra_body,
    )


async def build_audit_agent(settings: Settings):
    """Create OrchestratorAgent and ContextManager for document audit use."""
    from src.agent.agents.orch import OrchestratorAgent
    from src.agent.agents.parser import ParserAgent

    model = build_model_client(settings)

    agent = OrchestratorAgent(
        model=model,
        skills=OrchestratorAgent.DEFAULT_SKILLS,
        parser=ParserAgent(),
        exporter=None,
    )
    context_manager = ContextManager(model=model, cache_dir=settings.cache_dir)
    return agent, context_manager, model.model_name


async def build_chat_agent(settings: Settings):
    """Create a simple chat Agent for text-only conversations."""
    from src.agent.agents.base import Agent

    model = build_model_client(settings)

    agent = Agent(
        name="DocAudit助手",
        role=(
            "你是 DocAudit 文档审核平台的智能助手。"
            "你可以回答用户关于文档格式规范（GB/T 9704-2012）、"
            "内容审核、语法检查、排版建议等方面的问题。"
            "以专业、友好的方式提供帮助。"
        ),
        tools=[],
        model=model,
    )
    context_manager = ContextManager(model=model, cache_dir=settings.cache_dir)
    return agent, context_manager, model.model_name
```

- [ ] **Step 3: Verify**

Run: `uv run python -c "from src.agent.api.services.agent_service import build_model_client; print('OK')"`
Expected: prints "OK"

- [ ] **Step 4: Commit**

```bash
git add src/agent/api/services/
git commit -m "refactor: extract agent_service.py from routes.py"
```

---

### Task 6.2: Extract stream_service.py + session_service.py + file_service.py

**Files:**
- Create: `src/agent/api/services/stream_service.py`
- Create: `src/agent/api/services/session_service.py`
- Create: `src/agent/api/services/file_service.py`

- [ ] **Step 1: Create stream_service.py**

Extract from `routes.py` (lines 106-218): `_serialize_messages`, `_deserialize_messages`, `_reconstruct_state`, `_rehydrate_artifact_store`, `_TOOL_ARTIFACT_TYPE`, and the `event_generator` logic from the session route handler.

The `generate_sse_stream` function should accept all dependencies as parameters:
```python
"""SSE streaming service — event generation and artifact rehydration."""

from __future__ import annotations

import asyncio
import json
import logging
import time as _time
from pathlib import Path
from typing import AsyncGenerator

from src.config import Settings as _Settings
from ..session_store import SessionStore
from ..sse_adapter import SSEAdapter
from ...core.audit_logger import AuditLogger
from ...artifacts.store import ArtifactStore
from ...core.cache_store import CacheStore

logger = logging.getLogger(__name__)

_TOOL_ARTIFACT_TYPE: dict[str, str] = {
    "parse_document": "docaudit.parsed_document",
    "run_format_auditor": "docaudit.audit_finding_list",
    "run_content_auditor": "docaudit.audit_finding_list",
    "run_correction_auditor": "docaudit.audit_finding_list",
    "run_style_auditor": "docaudit.audit_finding_list",
    "run_plagiarism_auditor": "docaudit.plagiarism_report",
    "run_transform": "core.debug_view",
    "persist_output": "core.debug_view",
}


def serialize_messages(messages: tuple) -> str:
    """Serialize AgentState messages to a JSON string for persistence."""
    data = []
    for msg in messages:
        d: dict = {"role": msg.role, "content": msg.content}
        if msg.tool_calls:
            d["tool_calls"] = [
                {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                for tc in msg.tool_calls
            ]
        if msg.tool_call_id:
            d["tool_call_id"] = msg.tool_call_id
        if msg.name:
            d["name"] = msg.name
        data.append(d)
    return json.dumps(data, ensure_ascii=False)


def deserialize_messages(json_str: str) -> tuple:
    """Deserialize a JSON string back to a tuple of Message objects."""
    from ...core.state import Message as _Msg
    from ...core.model import ToolCall

    raw = json.loads(json_str)
    messages = []
    for d in raw:
        tool_calls = None
        if d.get("tool_calls"):
            tool_calls = [ToolCall(**tc) for tc in d["tool_calls"]]
        messages.append(_Msg(
            role=d["role"],
            content=d.get("content"),
            tool_calls=tool_calls,
            tool_call_id=d.get("tool_call_id"),
            name=d.get("name"),
        ))
    return tuple(messages)


def reconstruct_state(messages_json: str):
    """Reconstruct an AgentState from serialised messages, or None if empty."""
    from ...core.state import AgentState

    if not messages_json:
        return None
    messages = deserialize_messages(messages_json)
    if not messages:
        return None
    return AgentState(
        status="completed",
        messages=messages,
        current_step=0,
        max_steps=20,
    )


def rehydrate_artifact_store(
    artifact_store: ArtifactStore,
    messages: tuple,
    cache_store: CacheStore | None = None,
) -> None:
    """Scan prior messages for __persisted_output__ markers and re-register artifacts."""
    import json as _json

    for msg in messages:
        if msg.role != "tool":
            continue
        try:
            content = _json.loads(msg.content) if isinstance(msg.content, str) else msg.content
        except (_json.JSONDecodeError, TypeError):
            continue
        if not isinstance(content, dict):
            continue
        data = content.get("data")
        if not isinstance(data, dict) or not data.get("__persisted_output__"):
            continue

        ref_id = data.get("ref_id")
        filepath = data.get("file")
        tool_name = msg.name or ""
        if not ref_id or not filepath:
            continue

        artifact_type = _TOOL_ARTIFACT_TYPE.get(tool_name, "core.debug_view")

        try:
            artifact_data = _json.loads(Path(filepath).read_text(encoding="utf-8"))
        except (FileNotFoundError, _json.JSONDecodeError, OSError):
            continue

        artifact_store.register_cached_ref(
            ref_id=ref_id,
            artifact_type=artifact_type,
            created_by=tool_name,
            data=artifact_data,
            role="primary_document" if tool_name == "parse_document" else "intermediate",
            subject="current_upload" if tool_name == "parse_document" else "unknown",
            projection_allowed=True,
        )

        if cache_store is not None and ref_id and filepath:
            cache_store.ref_map[ref_id] = filepath


async def generate_sse_stream(
    *,
    agent: Any,
    session_id: str,
    task: str,
    agent_context: dict,
    session_store: SessionStore,
    settings: Any,
    prior_state: Any = None,
    is_new: bool = True,
    start_step: int = 0,
    context_manager: Any = None,
    model_name: str = "",
    pause_event: asyncio.Event = None,
    active_tasks: dict = None,
    audit_base_dir: str = "",
    audit_log_enabled: bool = True,
    tool_registry: Any = None,
) -> AsyncGenerator[str, None]:
    """Generate SSE event stream for an agent run.

    This is the core streaming logic extracted from the session route handler.
    """
    from ..sse_adapter import SSEAdapter

    queue: asyncio.Queue = asyncio.Queue()
    adapter = SSEAdapter(
        queue, session_store, session_id,
        pause_event or asyncio.Event(),
        start_step_index=start_step,
        tool_registry=tool_registry,
    )

    audit_logger = None
    if audit_log_enabled and audit_base_dir:
        audit_logger = AuditLogger.for_run(
            agent_name="api_session",
            base_dir=audit_base_dir,
            run_id=session_id,
        )

    # Emit session event for new sessions
    if is_new:
        yield f"data: {json.dumps({'type': 'session', 'sessionId': session_id, 'modelName': model_name}, ensure_ascii=False)}\n\n"
        await asyncio.sleep(0)

    async def runner():
        try:
            artifact_store = ArtifactStore()
            if prior_state is not None and prior_state.messages:
                cache_store = (
                    getattr(context_manager, "_cache", None)
                    if context_manager else None
                )
                rehydrate_artifact_store(
                    artifact_store, prior_state.messages, cache_store=cache_store
                )
            result = await agent.run(
                task=task,
                context=agent_context,
                on_step=adapter.on_step,
                on_token=adapter.on_token,
                on_content_token=adapter.on_content_token,
                on_tool_result=adapter.on_tool_result,
                context_manager=context_manager,
                state=prior_state,
                audit_logger=audit_logger,
                artifact_store=artifact_store,
            )
            flushed = adapter.flush_verdict()
            conclusion = flushed or (result.content or "")
            if result.final_state is not None:
                session_store.update(
                    session_id,
                    messages_json=serialize_messages(result.final_state.messages),
                )
            await queue.put(("complete", conclusion))
        except asyncio.CancelledError:
            logger.info("Agent run cancelled for session %s", session_id)
            session_store.update(session_id, status="stopped", finished_at=_time.time())
            await queue.put(("stopped", None))
        except Exception as exc:
            logger.exception("Agent run failed for session %s", session_id)
            await queue.put(("error", {"type": "error", "detail": str(exc)}))
        finally:
            await queue.put(("done", None))

    task_ref = asyncio.create_task(runner())
    if active_tasks is not None:
        active_tasks[session_id] = task_ref

    try:
        while True:
            item = await queue.get()
            tag = item[0]
            if tag == "done":
                break
            if tag == "event":
                yield item[1]
            elif tag == "complete":
                now = _time.time()
                conclusion = item[1] or ""
                session_store.update(
                    session_id,
                    status="completed",
                    finished_at=now,
                    conclusion=conclusion,
                )
                payload = {"type": "complete"}
                if conclusion:
                    payload["conclusion"] = conclusion
                session = session_store.get(session_id)
                if session:
                    payload["tokens_in"] = session.tokens_in
                    payload["tokens_out"] = session.tokens_out
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            elif tag == "stopped":
                payload = {"type": "stopped"}
                session = session_store.get(session_id)
                if session:
                    payload["tokens_in"] = session.tokens_in
                    payload["tokens_out"] = session.tokens_out
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            elif tag == "error":
                session_store.update(
                    session_id,
                    status="error",
                    finished_at=_time.time(),
                    error_detail=item[1].get("detail", ""),
                )
                payload = dict(item[1])
                session = session_store.get(session_id)
                if session:
                    payload["tokens_in"] = session.tokens_in
                    payload["tokens_out"] = session.tokens_out
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
    finally:
        if active_tasks is not None:
            active_tasks.pop(session_id, None)
```

- [ ] **Step 2: Create session_service.py**

```python
"""Session CRUD service functions."""

from __future__ import annotations

from ..session_store import SessionStore


def list_sessions(store: SessionStore):
    """List all historical sessions."""
    return store.list_all()


def get_session(store: SessionStore, session_id: str):
    """Get full detail for a session."""
    session = store.get(session_id)
    if session is None:
        return None
    return session.to_detail_dict()


def create_session(store: SessionStore, **kwargs):
    """Create a new session record."""
    return store.create(**kwargs)


def delete_session(store: SessionStore, session_id: str) -> bool:
    """Delete a session. Returns True if deleted, False if not found."""
    return store.delete(session_id)
```

- [ ] **Step 3: Create file_service.py**

```python
"""File upload service."""

from __future__ import annotations

import secrets
from datetime import datetime
from pathlib import Path

from fastapi import UploadFile

from src.config import Settings
from ..file_store import FileStore

ALLOWED_EXTS = {".pdf", ".docx", ".bmp", ".jpg", ".jpeg", ".png", ".gif", ".tif", ".tiff"}
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB


async def upload_file(
    file: UploadFile,
    settings: Settings,
    file_store: FileStore,
) -> dict:
    """Upload a document file, return fileId.

    Raises:
        ValueError: On invalid file type, empty file, or file too large.
    """
    if not file.filename:
        raise ValueError("文件为空")

    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTS:
        raise ValueError(f"不支持的文件格式: {ext}")

    contents = await file.read()
    if not contents:
        raise ValueError("文件内容为空")

    if len(contents) > MAX_FILE_SIZE:
        raise ValueError("文件过大")

    date_str = datetime.now().strftime("%Y/%m/%d")
    safe_name = f"doc_audit_{datetime.now().strftime('%Y%m%d%H%M%S')}_{secrets.token_hex(4)}{ext}"
    relative_path = f"{date_str}/{safe_name}"

    target_dir = Path(settings.upload_dir) / date_str
    target_dir.mkdir(parents=True, exist_ok=True)
    full_path = target_dir / safe_name
    full_path.write_bytes(contents)

    info = file_store.register(
        original_name=file.filename,
        stored_path=relative_path,
        size_bytes=len(contents),
    )

    return {"fileId": info.file_id}
```

- [ ] **Step 4: Commit**

```bash
git add src/agent/api/services/
git commit -m "refactor: extract stream, session, and file services from routes.py"
```

---

### Task 6.3: Split routes.py into routes/ package

**Files:**
- Create: `src/agent/api/routes/__init__.py`
- Create: `src/agent/api/routes/sessions.py`
- Create: `src/agent/api/routes/files.py`
- Create: `src/agent/api/routes/control.py`
- Modify: `src/agent/api/app.py` (update router import)
- Delete: `src/agent/api/routes.py`

- [ ] **Step 1: Create routes/__init__.py**

```python
"""API route modules — thin HTTP adapters delegating to services."""

from .sessions import router as sessions_router
from .files import router as files_router
from .control import router as control_router

# Combined router for app inclusion
from fastapi import APIRouter

router = APIRouter(prefix="/api")
router.include_router(sessions_router)
router.include_router(files_router)
router.include_router(control_router)
```

- [ ] **Step 2: Create routes/sessions.py**

Move session-related routes from `routes.py`:
- `GET /sessions` (lines 240-258, 260-477) — list + SSE streaming
- `GET /sessions/{session_id}` (lines 483-490)
- `DELETE /sessions/{session_id}` (lines 496-502)

Each route handler should:
1. Extract state from `request.app.state`
2. Call the appropriate service function
3. Return the result

```python
"""Session routes — list, create, continue, detail, delete."""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from ..services.session_service import list_sessions, get_session, delete_session
from ..services.agent_service import build_audit_agent, build_chat_agent
from ..services.stream_service import generate_sse_stream, reconstruct_state

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/sessions")
async def handle_sessions(
    request: Request,
    task: Optional[str] = Query(default=None),
    fileId: Optional[str] = Query(default=None),
    sessionId: Optional[str] = Query(default=None),
):
    """List sessions, create new, or continue existing."""
    state = request.app.state
    settings = state.settings
    session_store = state.session_store

    # List mode
    if task is None and fileId is None and sessionId is None:
        return list_sessions(session_store)

    if not task:
        raise HTTPException(400, "task 参数必须提供")

    pause_event = state.pause_event

    if sessionId:
        # Continue existing session
        existing = session_store.get(sessionId)
        if existing is None:
            raise HTTPException(404, "会话不存在")

        prior_state = reconstruct_state(existing.messages_json)
        start_step = len(existing.steps)
        is_new = False

        if existing.file_id:
            file_store = state.file_store
            file_path = file_store.resolve_path(existing.file_id, settings.upload_dir)
            if file_path is None or not file_path.exists():
                raise HTTPException(404, f"文件不存在: {existing.file_id}")
            try:
                agent, context_manager, model_name = await build_audit_agent(settings)
            except Exception as exc:
                raise HTTPException(500, f"Failed to build agent: {exc}")
            agent_context = {"file_path": str(file_path)}
        else:
            try:
                agent, context_manager, model_name = await build_chat_agent(settings)
            except Exception as exc:
                raise HTTPException(500, f"Failed to build chat agent: {exc}")
            agent_context = {}

        session_store.update(sessionId, status="running", error_detail=None)
    else:
        # New session
        import secrets
        session_id = f"sess_{secrets.token_hex(6)}"
        prior_state = None
        start_step = 0

        if fileId:
            file_store = state.file_store
            file_path = file_store.resolve_path(fileId, settings.upload_dir)
            if file_path is None or not file_path.exists():
                raise HTTPException(404, f"文件不存在: {fileId}")

            file_info = file_store.resolve(fileId)
            file_name = file_info.original_name if file_info else ""

            try:
                agent, context_manager, model_name = await build_audit_agent(settings)
            except Exception as exc:
                raise HTTPException(500, f"Failed to build agent: {exc}")

            agent_context = {"file_path": str(file_path)}
        else:
            file_name = ""

            try:
                agent, context_manager, model_name = await build_chat_agent(settings)
            except Exception as exc:
                raise HTTPException(500, f"Failed to build chat agent: {exc}")

            agent_context = {}

        is_new = True

        session_store.create(
            session_id=session_id,
            task=task,
            file_id=fileId or "",
            file_name=file_name,
            model_name=model_name,
        )

    # Build agent context
    agent_context["audit_base_dir"] = settings.audit_log_dir

    return StreamingResponse(
        generate_sse_stream(
            agent=agent,
            session_id=session_id,
            task=task,
            agent_context=agent_context,
            session_store=session_store,
            settings=settings,
            prior_state=prior_state,
            is_new=is_new,
            start_step=start_step,
            context_manager=context_manager,
            model_name=model_name,
            pause_event=pause_event,
            active_tasks=state.active_tasks,
            audit_base_dir=settings.audit_log_dir,
            audit_log_enabled=settings.audit_log_enabled,
            tool_registry=getattr(state, "tool_registry", None),
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/sessions/{session_id}")
async def handle_session_detail(session_id: str, request: Request):
    """Get session detail."""
    session = get_session(request.app.state.session_store, session_id)
    if session is None:
        raise HTTPException(404, "会话不存在")
    return session


@router.delete("/sessions/{session_id}")
async def handle_session_delete(session_id: str, request: Request):
    """Delete a session."""
    if not delete_session(request.app.state.session_store, session_id):
        raise HTTPException(404, "会话不存在")
    return {"status": "ok"}
```

- [ ] **Step 3: Create routes/files.py**

```python
"""File upload route."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, UploadFile

from ..services.file_service import upload_file

router = APIRouter()


@router.post("/files")
async def handle_upload(request: Request, file: UploadFile):
    """Upload a document file."""
    state = request.app.state
    try:
        return await upload_file(
            file=file,
            settings=state.settings,
            file_store=state.file_store,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
```

- [ ] **Step 4: Create routes/control.py**

```python
"""Session control routes — pause, resume, stop."""

from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request

router = APIRouter()


@router.post("/pause")
async def handle_pause(request: Request):
    """Pause the currently running session."""
    request.app.state.pause_event.set()
    return {"status": "ok"}


@router.post("/resume")
async def handle_resume(request: Request):
    """Resume a paused session."""
    request.app.state.pause_event.clear()
    return {"status": "ok"}


@router.post("/stop")
async def handle_stop(
    request: Request,
    sessionId: Optional[str] = Query(default=None),
):
    """Stop a running session."""
    active_tasks: dict[str, asyncio.Task] = request.app.state.active_tasks

    if sessionId:
        task = active_tasks.get(sessionId)
        if task is None:
            raise HTTPException(404, f"会话 {sessionId} 未在运行")
        task.cancel()
        return {"status": "ok", "sessionId": sessionId, "stopped": True}

    if not active_tasks:
        return {"status": "ok", "stopped": 0, "message": "没有正在运行的任务"}

    stopped = []
    for sid, task in list(active_tasks.items()):
        task.cancel()
        stopped.append(sid)

    return {"status": "ok", "stoppedCount": len(stopped), "sessionIds": stopped}
```

- [ ] **Step 5: Update app.py router import**

In `src/agent/api/app.py`, change:
```python
from .routes import router
```
To:
```python
from .routes import router
```
(This import should still work since `routes/__init__.py` exports `router`)

- [ ] **Step 6: Delete old routes.py and run tests**

```bash
rm src/agent/api/routes.py
uv run pytest tests/agent/api/ -v --timeout=60 2>&1 | tail -20
```
Expected: All tests pass (or need import path updates in tests)

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "refactor: split routes.py into routes/ package with service layer"
```

---

### Task 7.1: Document plugin/skill boundary

**Files:**
- Create: `docs/architecture/plugin-skill-boundary.md`
- Modify: `src/agent/skills/__init__.py` (add docstring)
- Modify: `src/plugin/__init__.py` (add docstring)

- [ ] **Step 1: Create architecture documentation**

Create `docs/architecture/plugin-skill-boundary.md`:

```markdown
# Plugin vs Skill — Boundary

## Skills (`src/agent/skills/`)

**Purpose:** Built-in capabilities loaded as Python modules.
**Characteristics:**
- In-process execution (no isolation)
- Access to full Python API
- Provides: ToolProtocol implementations + LLM system prompts + reference docs
- Defined by SKILL.md files with frontmatter metadata
- Loaded by SkillLoader at agent construction time

**When to use Skills:**
- Core audit functionality (format, content, style, plagiarism, correction)
- Any tool that needs direct access to domain modules
- Functionality that is tightly coupled to the application

## Plugins (`src/plugin/`)

**Purpose:** Third-party / community extensions with process isolation.
**Characteristics:**
- Subprocess execution (JSON-RPC over stdio)
- Sandboxed — cannot access application internals directly
- Provides: ToolProtocol via ProxyTool, ProxyChecker, ProxyAgent
- Defined by plugin.json manifests in the plugins/ directory
- Managed by ProcessManager with lifecycle (start/stop/restart)

**When to use Plugins:**
- Third-party extensions from external developers
- Untrusted code that needs sandboxing
- Functionality that should be independently deployable
- Long-running services that need process-level isolation

## ToolRegistry Integration

Both Skills and Plugins ultimately register into ToolRegistry.
Callers (Agent, HTTP routes, CLI) use tools without knowing their source.
```

- [ ] **Step 2: Update module docstrings**

In `src/agent/skills/__init__.py`, add/update docstring:
```python
"""Built-in skills — Python modules loaded in-process via SkillLoader.

See docs/architecture/plugin-skill-boundary.md for Skill vs Plugin guidance.
"""
```

In `src/plugin/__init__.py`, update docstring:
```python
"""DocAudit Plugin System — subprocess-isolated extensions via JSON-RPC.

See docs/architecture/plugin-skill-boundary.md for Skill vs Plugin guidance.
"""
```

- [ ] **Step 3: Commit**

```bash
git add docs/architecture/ src/agent/skills/__init__.py src/plugin/__init__.py
git commit -m "docs: document plugin vs skill boundary and responsibilities"
```

---

### Task 7.2: Clean up remaining inline imports

**Files:**
- Various — search and fix

- [ ] **Step 1: Find remaining inline imports**

Run: `grep -rn "from src\.\|from \.\." src/ --include="*.py" | grep -v "__init__.py" | grep -v "TYPE_CHECKING" | grep "def \|async def " -B1 | grep "import\|from"`

More practically, search for import statements inside function bodies:
Run: `cd /home/lmwl/Documents/docaudit/docaudit-agent && python3 -c "
import ast, os, sys

def find_inline_imports(filepath):
    with open(filepath) as f:
        try:
            tree = ast.parse(f.read())
        except SyntaxError:
            return []
    results = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            # Check if inside a function
            for parent in ast.walk(tree):
                if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for child in ast.walk(parent):
                        if child is node:
                            results.append((filepath, parent.lineno, parent.name))
                            break
    return results

for root, dirs, files in os.walk('src'):
    dirs[:] = [d for d in dirs if d != '__pycache__']
    for f in files:
        if f.endswith('.py'):
            results = find_inline_imports(os.path.join(root, f))
            for r in results:
                print(f'{r[0]}:{r[1]} in {r[2]}()')
" 2>&1 | head -30
`
Expected: Shows remaining inline imports for manual review

- [ ] **Step 2: Fix remaining inline imports**

For each remaining inline import, determine if it can be moved to module level:
- If the import causes a circular import at module level → keep inline, add `# pragma: inline-import` comment
- If it can be moved to module level safely → move it to top of file
- If it's inside `loop_hints.py` functions → these are expected (artifact resolver imports), add comment

- [ ] **Step 3: Run full test suite**

Run: `uv run pytest tests/ -v --timeout=120 -m "not integration" 2>&1 | tail -30`
Expected: All non-integration tests pass

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "refactor: clean up remaining inline imports"
```

---

### Task 7.3: Final integration verification

**Files:**
- All — final verification

- [ ] **Step 1: Run full test suite**

```bash
uv run pytest tests/ -v --timeout=120 -m "not integration" 2>&1 | tail -40
```
Expected: All tests pass

- [ ] **Step 2: Verify app factory still works**

```bash
uv run python -c "
from src.agent.api.app import create_app
app = create_app()
print('Routes:', [r.path for r in app.routes])
print('Tools:', len(app.state.tool_registry.list_tools()))
print('OK')
"
```
Expected: prints routes list, tool count > 0, "OK"

- [ ] **Step 3: Verify all file size limits**

```bash
python3 -c "
import os
for root, dirs, files in os.walk('src'):
    dirs[:] = [d for d in dirs if d != '__pycache__']
    for f in files:
        if f.endswith('.py'):
            path = os.path.join(root, f)
            lines = len(open(path).readlines())
            if lines > 800:
                print(f'WARNING: {lines} lines in {path}')
"
```
Expected: No output (no files exceed 800 lines)

- [ ] **Step 4: Final commit**

```bash
git add -A
git commit -m "chore: final verification — all tests pass, file limits met"
```
