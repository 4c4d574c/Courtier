# Tool Output Reference Resolution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist all tool outputs to disk and resolve `$ref:<tool_name>:<sequence>` references automatically before tool execution, removing the need for `_PASSTHROUGH_TOOLS`.

**Architecture:** `ContextManager` assigns semantic IDs to persisted outputs and maintains a mapping table. `ToolRegistry.execute()` resolves references via `resolve_refs()` before delegating to the tool. A short instruction block is injected into the system prompt so the LLM knows how to use ref IDs.

**Tech Stack:** Python 3.12, pytest, pydantic

---

## File Structure

| File | Responsibility |
|------|---------------|
| `src/agent/core/context_manager.py` | Ref ID generation, mapping table, `resolve_refs()`, `get_ref_instructions()`, remove `_PASSTHROUGH_TOOLS` |
| `src/agent/tools/registry.py` | Add `context_manager` param to `execute()` |
| `src/agent/core/loop.py` | Pass `context_manager` to `tool_registry.execute()` |
| `src/agent/agents/base.py` | Inject ref instructions into system prompt |
| `tests/agent/test_context_manager.py` | Update passthrough tests, add ref resolution tests |
| `tests/agent/test_loop.py` | Update tests for new `execute()` signature |
| `tests/agent/test_registry.py` | Add tests for `execute()` with context_manager |

---

### Task 1: Add ref ID mapping table to `CompactState` and `ContextManager`

**Files:**
- Modify: `src/agent/core/context_manager.py:53-59` (CompactState)
- Modify: `src/agent/core/context_manager.py:83-91` (ContextManager.__init__)
- Test: `tests/agent/test_context_manager.py:199-205` (TestCompactState)

- [ ] **Step 1: Write the failing test**

Add to `tests/agent/test_context_manager.py`, update `TestCompactState`:

```python
class TestCompactState:
    def test_default_values(self):
        state = CompactState()
        assert state.has_compacted is False
        assert state.last_summary is None
        assert state.recent_files == []
        assert state.compact_count == 0
        assert state.ref_map == {}
        assert state.ref_counters == {}

    def test_ref_map_tracks_ids(self):
        state = CompactState()
        state.ref_map["$ref:parse_document:1"] = ".agent_cache/parse_document_123.json"
        assert "$ref:parse_document:1" in state.ref_map
        assert state.ref_map["$ref:parse_document:1"] == ".agent_cache/parse_document_123.json"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/lmwl/Documents/docaudit/docaudit-backend/.worktrees/agent-phase0 && python -m pytest tests/agent/test_context_manager.py::TestCompactState -v`
Expected: FAIL — `CompactState` has no `ref_map` or `ref_counters` attributes.

- [ ] **Step 3: Add `ref_map` and `ref_counters` to `CompactState`**

In `src/agent/core/context_manager.py`, update `CompactState`:

```python
@dataclass
class CompactState:
    """Tracks compaction status across the agent loop."""

    has_compacted: bool = False
    last_summary: str | None = None
    recent_files: list[str] = field(default_factory=list)
    compact_count: int = 0
    ref_map: dict[str, str] = field(default_factory=dict)
    ref_counters: dict[str, int] = field(default_factory=dict)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/lmwl/Documents/docaudit/docaudit-backend/.worktrees/agent-phase0 && python -m pytest tests/agent/test_context_manager.py::TestCompactState -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/agent/core/context_manager.py tests/agent/test_context_manager.py
git commit -m "feat: add ref_map and ref_counters to CompactState"
```

---

### Task 2: Generate semantic ref IDs in `persist_large_output`

**Files:**
- Modify: `src/agent/core/context_manager.py:30-50` (remove `_PASSTHROUGH_TOOLS`)
- Modify: `src/agent/core/context_manager.py:84-91` (remove `passthrough_tools` param)
- Modify: `src/agent/core/context_manager.py:96-138` (add ref ID generation)
- Test: `tests/agent/test_context_manager.py:42-93` (TestLayer1PersistLargeOutput)

- [ ] **Step 1: Write the failing test**

Replace the entire `TestLayer1PersistLargeOutput` class in `tests/agent/test_context_manager.py`:

```python
class TestLayer1PersistLargeOutput:
    def test_small_data_passes_through(self, mgr):
        data = {"key": "value"}
        result = mgr.persist_large_output("test_tool", data)
        assert result == data

    def test_large_data_persisted_with_ref_id(self, mgr):
        data = {"text": "x" * 1000}
        result = mgr.persist_large_output("search_documents", data)

        assert isinstance(result, dict)
        assert result["__persisted_output__"] is True
        assert result["ref_id"] == "$ref:search_documents:1"
        assert "file" in result
        assert result["size_chars"] > 900
        assert "preview" in result

    def test_ref_id_increments_per_tool(self, mgr):
        data = {"text": "x" * 1000}
        r1 = mgr.persist_large_output("search_documents", data)
        r2 = mgr.persist_large_output("search_documents", data)
        r3 = mgr.persist_large_output("other_tool", data)

        assert r1["ref_id"] == "$ref:search_documents:1"
        assert r2["ref_id"] == "$ref:search_documents:2"
        assert r3["ref_id"] == "$ref:other_tool:1"

    def test_ref_map_tracks_all_persisted_outputs(self, mgr):
        data = {"text": "x" * 1000}
        r1 = mgr.persist_large_output("search_documents", data)
        r2 = mgr.persist_large_output("other_tool", data)

        assert mgr.state.ref_map[r1["ref_id"]] == r1["file"]
        assert mgr.state.ref_map[r2["ref_id"]] == r2["file"]

    def test_all_tools_persisted_no_passthrough(self, mgr):
        """ALL tools are now persisted, including audit tools."""
        for tool_name in ["parse_document", "audit_format", "audit_content"]:
            data = {"text": "x" * 1000}
            result = mgr.persist_large_output(tool_name, data)
            assert isinstance(result, dict)
            assert result["__persisted_output__"] is True

    def test_none_data_passes_through(self, mgr):
        result = mgr.persist_large_output("test_tool", None)
        assert result is None

    def test_files_tracked_in_state(self, mgr):
        data = {"text": "x" * 1000}
        mgr.persist_large_output("tool_a", data)
        assert len(mgr.state.recent_files) == 1

    def test_file_on_disk_matches_data(self, mgr):
        import json
        data = {"text": "x" * 1000}
        result = mgr.persist_large_output("search_documents", data)

        filepath = result["file"]
        with open(filepath, encoding="utf-8") as f:
            loaded = json.load(f)
        assert loaded == data
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/lmwl/Documents/docaudit/docaudit-backend/.worktrees/agent-phase0 && python -m pytest tests/agent/test_context_manager.py::TestLayer1PersistLargeOutput -v`
Expected: FAIL — `test_large_data_persisted_with_ref_id` expects `ref_id` field, `test_all_tools_persisted_no_passthrough` expects audit tools to be persisted.

- [ ] **Step 3: Remove `_PASSTHROUGH_TOOLS` and update `persist_large_output`**

In `src/agent/core/context_manager.py`:

1. Delete the `_PASSTHROUGH_TOOLS` frozenset (lines 30-50).
2. Remove `passthrough_tools` parameter from `ContextManager.__init__` and the `self._passthrough` attribute (lines 83, 91).
3. Replace `persist_large_output` method (lines 96-138) with:

```python
def persist_large_output(self, tool_name: str, data: object) -> object:
    """If data is large, save to disk and return a preview marker with ref_id.

    Returns the original data if small, or a persisted-output dict with ref_id.
    """
    if data is None:
        return data

    try:
        serialized = json.dumps(data, ensure_ascii=False)
    except (TypeError, ValueError):
        return data

    if len(serialized) <= self.large_output_threshold:
        return data

    # Generate semantic ref ID: $ref:<tool_name>:<sequence>
    seq = self.state.ref_counters.get(tool_name, 0) + 1
    self.state.ref_counters[tool_name] = seq
    ref_id = f"$ref:{tool_name}:{seq}"

    # Save full output to disk
    import time
    ts = int(time.time() * 1000)
    safe_name = tool_name.replace("/", "_").replace(" ", "_")
    filename = f"{safe_name}_{ts}.json"
    filepath = self._cache_dir / filename
    filepath.write_text(serialized, encoding="utf-8")
    self.state.recent_files.append(str(filepath))

    # Register in mapping table
    self.state.ref_map[ref_id] = str(filepath)

    # Return a preview marker with ref_id
    preview_chars = min(2000, len(serialized))
    preview = serialized[:preview_chars]
    if preview_chars < len(serialized):
        preview += f"\n...[truncated, full output ({len(serialized)} chars) saved to {filepath}]"

    return {
        "__persisted_output__": True,
        "ref_id": ref_id,
        "file": str(filepath),
        "size_chars": len(serialized),
        "preview": preview,
    }
```

4. Update any call sites that pass `passthrough_tools=` to `ContextManager()`.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/lmwl/Documents/docaudit/docaudit-backend/.worktrees/agent-phase0 && python -m pytest tests/agent/test_context_manager.py::TestLayer1PersistLargeOutput -v`
Expected: PASS

- [ ] **Step 5: Run full test suite to check for regressions**

Run: `cd /home/lmwl/Documents/docaudit/docaudit-backend/.worktrees/agent-phase0 && python -m pytest tests/agent/ -v`
Expected: All PASS. If any test passes `passthrough_tools=` to `ContextManager`, update the call site to remove it.

- [ ] **Step 6: Commit**

```bash
git add src/agent/core/context_manager.py tests/agent/test_context_manager.py
git commit -m "feat: generate semantic ref IDs in persist_large_output, remove PASSTHROUGH_TOOLS"
```

---

### Task 3: Add `resolve_refs()` to `ContextManager`

**Files:**
- Modify: `src/agent/core/context_manager.py` (add method after `persist_large_output`)
- Test: `tests/agent/test_context_manager.py` (add new test class)

- [ ] **Step 1: Write the failing test**

Add new test class to `tests/agent/test_context_manager.py`:

```python
import re


class TestResolveRefs:
    def test_resolves_ref_string_to_disk_data(self, mgr):
        """A $ref:tool:N string in kwargs is replaced with the loaded JSON."""
        data = {"pages": [{"text": "full document"}]}
        mgr.persist_large_output("parse_document", data)

        kwargs = {"document": "$ref:parse_document:1", "doc_type": "通知"}
        resolved = mgr.resolve_refs(kwargs)

        assert resolved["document"] == data
        assert resolved["doc_type"] == "通知"

    def test_no_refs_returns_unchanged(self, mgr):
        kwargs = {"text": "hello", "count": 5}
        resolved = mgr.resolve_refs(kwargs)
        assert resolved == kwargs

    def test_unknown_ref_keeps_original_string(self, mgr):
        kwargs = {"document": "$ref:nonexistent:99"}
        resolved = mgr.resolve_refs(kwargs)
        assert resolved["document"] == "$ref:nonexistent:99"

    def test_resolves_nested_ref_in_list(self, mgr):
        data = {"result": "big audit output"}
        mgr.persist_large_output("audit_format", data)

        kwargs = {"items": ["$ref:audit_format:1", "plain_text"]}
        resolved = mgr.resolve_refs(kwargs)

        assert resolved["items"][0] == data
        assert resolved["items"][1] == "plain_text"

    def test_resolves_nested_ref_in_dict(self, mgr):
        data = {"pages": []}
        mgr.persist_large_output("parse_document", data)

        kwargs = {"outer": {"doc": "$ref:parse_document:1"}}
        resolved = mgr.resolve_refs(kwargs)

        assert resolved["outer"]["doc"] == data

    def test_multiple_refs_in_one_call(self, mgr):
        data1 = {"pages": []}
        data2 = {"violations": []}
        mgr.persist_large_output("parse_document", data1)
        mgr.persist_large_output("audit_format", data2)

        kwargs = {
            "document": "$ref:parse_document:1",
            "audit_result": "$ref:audit_format:1",
        }
        resolved = mgr.resolve_refs(kwargs)

        assert resolved["document"] == data1
        assert resolved["audit_result"] == data2

    def test_non_ref_dollar_sign_not_touched(self, mgr):
        kwargs = {"price": "$100", "name": "test"}
        resolved = mgr.resolve_refs(kwargs)
        assert resolved["price"] == "$100"

    def test_empty_kwargs(self, mgr):
        resolved = mgr.resolve_refs({})
        assert resolved == {}

    def test_file_missing_falls_back_gracefully(self, mgr):
        """If disk file is deleted, ref keeps original string."""
        data = {"text": "x" * 1000}
        mgr.persist_large_output("search_documents", data)

        # Delete the file
        ref_id = "$ref:search_documents:1"
        filepath = mgr.state.ref_map[ref_id]
        import os
        os.remove(filepath)

        kwargs = {"data": ref_id}
        resolved = mgr.resolve_refs(kwargs)
        assert resolved["data"] == ref_id
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/lmwl/Documents/docaudit/docaudit-backend/.worktrees/agent-phase0 && python -m pytest tests/agent/test_context_manager.py::TestResolveRefs -v`
Expected: FAIL — `AttributeError: 'ContextManager' object has no attribute 'resolve_refs'`

- [ ] **Step 3: Implement `resolve_refs()`**

Add to `src/agent/core/context_manager.py`, after `persist_large_output` and before `micro_compact`:

```python
_REF_PATTERN = re.compile(r"^\$ref:([a-z_]+):(\d+)$")

def resolve_refs(self, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Recursively resolve $ref:tool:N references in tool call arguments.

    Returns a new dict with ref strings replaced by data loaded from disk.
    Unknown refs and missing files are kept as-is with a warning log.
    """
    return self._resolve_value(kwargs)

def _resolve_value(self, value: Any) -> Any:
    if isinstance(value, str):
        match = _REF_PATTERN.match(value)
        if match:
            ref_id = value
            filepath = self.state.ref_map.get(ref_id)
            if filepath is None:
                logger.warning("Unknown ref_id: %s", ref_id)
                return value
            return self._load_ref(ref_id, filepath)
        return value
    if isinstance(value, dict):
        return {k: self._resolve_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [self._resolve_value(v) for v in value]
    return value

def _load_ref(self, ref_id: str, filepath: str) -> Any:
    """Load JSON data from disk for a ref_id. Returns original string on failure."""
    try:
        return json.loads(Path(filepath).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to load ref %s from %s: %s", ref_id, filepath, exc)
        return ref_id
```

Also add `import re` at the top if not already present.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/lmwl/Documents/docaudit/docaudit-backend/.worktrees/agent-phase0 && python -m pytest tests/agent/test_context_manager.py::TestResolveRefs -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/agent/core/context_manager.py tests/agent/test_context_manager.py
git commit -m "feat: add resolve_refs() to ContextManager for $ref resolution"
```

---

### Task 4: Add `get_ref_instructions()` to `ContextManager`

**Files:**
- Modify: `src/agent/core/context_manager.py` (add method)
- Test: `tests/agent/test_context_manager.py` (add test)

- [ ] **Step 1: Write the failing test**

Add to `tests/agent/test_context_manager.py`:

```python
class TestGetRefInstructions:
    def test_returns_instruction_string(self, mgr):
        instructions = mgr.get_ref_instructions()
        assert "$ref:" in instructions
        assert "ref_id" in instructions
        assert "__persisted_output__" in instructions

    def test_instruction_is_short(self, mgr):
        instructions = mgr.get_ref_instructions()
        assert len(instructions) < 200
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/lmwl/Documents/docaudit/docaudit-backend/.worktrees/agent-phase0 && python -m pytest tests/agent/test_context_manager.py::TestGetRefInstructions -v`
Expected: FAIL — `AttributeError`

- [ ] **Step 3: Implement `get_ref_instructions()`**

Add to `src/agent/core/context_manager.py`:

```python
def get_ref_instructions(self) -> str:
    """Return a short instruction block for the LLM about how to use ref IDs."""
    return (
        "当工具返回包含 __persisted_output__ 标记的结果时，完整数据已存入磁盘。"
        "如需将此数据作为后续工具的参数传入，请使用 ref_id 的值"
        '（如 "$ref:parse_document:1"）作为参数值。'
        "系统会自动加载完整数据替换引用。"
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/lmwl/Documents/docaudit/docaudit-backend/.worktrees/agent-phase0 && python -m pytest tests/agent/test_context_manager.py::TestGetRefInstructions -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/agent/core/context_manager.py tests/agent/test_context_manager.py
git commit -m "feat: add get_ref_instructions() to ContextManager"
```

---

### Task 5: Add `context_manager` param to `ToolRegistry.execute()`

**Files:**
- Modify: `src/agent/tools/registry.py:53-56`
- Test: `tests/agent/test_registry.py` (add tests)

- [ ] **Step 1: Write the failing test**

Add to `tests/agent/test_registry.py`:

```python
from unittest.mock import MagicMock


class TestExecuteWithRefResolution:
    @pytest.mark.asyncio
    async def test_execute_resolves_refs(self, registry_with_fake):
        """When context_manager is provided, resolve_refs is called on kwargs."""
        mock_cm = MagicMock()
        mock_cm.resolve_refs.return_value = {"value": "resolved_data"}

        result = await registry_with_fake.execute(
            "fake", context_manager=mock_cm, value="$ref:some_tool:1"
        )

        mock_cm.resolve_refs.assert_called_once_with({"value": "$ref:some_tool:1"})
        assert result.success
        assert result.data == "resolved_data"

    @pytest.mark.asyncio
    async def test_execute_without_context_manager(self, registry_with_fake):
        """When context_manager is None, kwargs pass through unchanged."""
        result = await registry_with_fake.execute("fake", value="hello")
        assert result.success
        assert result.data == "hello"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/lmwl/Documents/docaudit/docaudit-backend/.worktrees/agent-phase0 && python -m pytest tests/agent/test_registry.py::TestExecuteWithRefResolution -v`
Expected: FAIL — `TypeError: execute() got an unexpected keyword argument 'context_manager'`

- [ ] **Step 3: Update `ToolRegistry.execute()`**

Replace `src/agent/tools/registry.py:53-56` with:

```python
async def execute(
    self,
    name: str,
    context_manager: Any | None = None,
    **kwargs: Any,
) -> ToolResult:
    """Execute a tool by name with the given arguments.

    If context_manager is provided, resolve $ref references in kwargs first.
    """
    if context_manager is not None:
        kwargs = context_manager.resolve_refs(kwargs)
    tool = self.get(name)
    return await tool.execute(**kwargs)
```

Add at the top of the file:

```python
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..core.context_manager import ContextManager
```

Note: `Any` is used instead of `ContextManager` at runtime to avoid circular imports.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/lmwl/Documents/docaudit/docaudit-backend/.worktrees/agent-phase0 && python -m pytest tests/agent/test_registry.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/agent/tools/registry.py tests/agent/test_registry.py
git commit -m "feat: add context_manager param to ToolRegistry.execute for ref resolution"
```

---

### Task 6: Pass `context_manager` through `agent_loop` to `tool_registry.execute()`

**Files:**
- Modify: `src/agent/core/loop.py:131-133`
- Test: `tests/agent/test_loop.py` (update existing tests if needed)

- [ ] **Step 1: Write the failing test**

Add to `tests/agent/test_loop.py`:

```python
from unittest.mock import MagicMock, AsyncMock


class TestLoopRefResolution:
    @pytest.mark.asyncio
    async def test_context_manager_passed_to_execute(self):
        """agent_loop passes context_manager to tool_registry.execute."""
        from src.agent.tools.protocol import ToolResult as TR

        tc = ToolCall(id="call_1", name="echo", arguments={"text": "hello"})
        model = MockModelClient(tool_calls=[tc])

        registry = ToolRegistry()
        mock_execute = AsyncMock(return_value=TR(success=True, data="echoed"))

        # Patch the registry's execute to verify it receives context_manager
        registry.execute = mock_execute

        mock_cm = MagicMock()

        state = AgentState.initial(task="echo hello")
        await agent_loop(
            state=state,
            model=model,
            tool_registry=registry,
            context_manager=mock_cm,
        )

        mock_execute.assert_called_once()
        call_kwargs = mock_execute.call_args
        assert call_kwargs.kwargs.get("context_manager") is mock_cm or \
               (len(call_kwargs.args) > 1 and call_kwargs.args[1] is mock_cm)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/lmwl/Documents/docaudit/docaudit-backend/.worktrees/agent-phase0 && python -m pytest tests/agent/test_loop.py::TestLoopRefResolution -v`
Expected: FAIL — `context_manager` is not passed through to `execute()`.

- [ ] **Step 3: Update `agent_loop` to pass `context_manager`**

In `src/agent/core/loop.py`, change line 133:

```python
result = await tool_registry.execute(
    tool_call.name, **tool_call.arguments
)
```

to:

```python
result = await tool_registry.execute(
    tool_call.name,
    context_manager=context_manager,
    **tool_call.arguments,
)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/lmwl/Documents/docaudit/docaudit-backend/.worktrees/agent-phase0 && python -m pytest tests/agent/test_loop.py::TestLoopRefResolution -v`
Expected: PASS

- [ ] **Step 5: Run full agent loop tests**

Run: `cd /home/lmwl/Documents/docaudit/docaudit-backend/.worktrees/agent-phase0 && python -m pytest tests/agent/test_loop.py -v`
Expected: All PASS.

- [ ] **Step 6: Commit**

```bash
git add src/agent/core/loop.py tests/agent/test_loop.py
git commit -m "feat: pass context_manager to tool_registry.execute in agent_loop"
```

---

### Task 7: Inject ref instructions into system prompt

**Files:**
- Modify: `src/agent/agents/base.py:122-149` (`run` method)
- Test: `tests/agent/test_context_manager.py` or `tests/agent/agents/test_base.py` (if exists)

- [ ] **Step 1: Write the failing test**

Check if `tests/agent/agents/test_base.py` exists. If not, create `tests/agent/agents/test_base.py`:

```python
"""Tests for Agent base class ref instruction injection."""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from src.agent.agents.base import Agent


@pytest.fixture
def agent_with_cm():
    """An Agent with a mock context_manager."""
    agent = Agent(
        name="TestAgent",
        role="You are a test agent.",
        model=MagicMock(),
    )
    mock_cm = MagicMock()
    mock_cm.get_ref_instructions.return_value = (
        'Use $ref:tool:N as parameter value to reference persisted outputs.'
    )
    return agent, mock_cm


class TestRefInstructionInjection:
    @pytest.mark.asyncio
    async def test_system_prompt_includes_ref_instructions(self, agent_with_cm):
        agent, mock_cm = agent_with_cm

        # Patch agent_loop to capture the state
        captured_state = None

        async def _capture_loop(**kwargs):
            nonlocal captured_state
            from src.agent.core.state import AgentState
            captured_state = kwargs["state"]
            return AgentState(status="completed", messages=())

        import src.agent.core.loop as loop_mod
        original = loop_mod.agent_loop
        loop_mod.agent_loop = _capture_loop

        try:
            await agent.run("test task", context_manager=mock_cm)
            assert captured_state is not None
            system_msg = captured_state.messages[0]
            assert "$ref:" in system_msg.content
        finally:
            loop_mod.agent_loop = original

    @pytest.mark.asyncio
    async def test_no_injection_without_context_manager(self, agent_with_cm):
        agent, _ = agent_with_cm

        captured_state = None

        async def _capture_loop(**kwargs):
            nonlocal captured_state
            from src.agent.core.state import AgentState
            captured_state = kwargs["state"]
            return AgentState(status="completed", messages=())

        import src.agent.core.loop as loop_mod
        original = loop_mod.agent_loop
        loop_mod.agent_loop = _capture_loop

        try:
            await agent.run("test task")
            assert captured_state is not None
            system_msg = captured_state.messages[0]
            assert "$ref:" not in system_msg.content
        finally:
            loop_mod.agent_loop = original
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/lmwl/Documents/docaudit/docaudit-backend/.worktrees/agent-phase0 && python -m pytest tests/agent/agents/test_base.py::TestRefInstructionInjection -v`
Expected: FAIL — system prompt does not contain `$ref:`.

- [ ] **Step 3: Update `Agent.run()` to inject ref instructions**

In `src/agent/agents/base.py`, update the `run` method:

```python
async def run(
    self,
    task: str,
    context: dict[str, str] | None = None,
    on_step: Callable[[str, str], Awaitable[None]] | None = None,
    on_token: Callable[[str], Awaitable[None]] | None = None,
    context_manager: Any | None = None,
) -> AgentResult:
    """Entry point: receive task, run agent loop, return result."""
    system_prompt = self._build_system_prompt(context)

    if context_manager is not None:
        system_prompt += "\n\n" + context_manager.get_ref_instructions()

    state = AgentState.initial(
        task=task,
        system_prompt=system_prompt,
    )
    final_state = await agent_loop(
        state=state,
        model=self.model,
        tool_registry=self.tool_registry,
        hooks=self.hooks,
        permissions=self.permissions,
        on_step=on_step,
        on_token=on_token,
        context_manager=context_manager,
    )
    return AgentResult.from_state(final_state)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/lmwl/Documents/docaudit/docaudit-backend/.worktrees/agent-phase0 && python -m pytest tests/agent/agents/test_base.py::TestRefInstructionInjection -v`
Expected: PASS

- [ ] **Step 5: Run full test suite**

Run: `cd /home/lmwl/Documents/docaudit/docaudit-backend/.worktrees/agent-phase0 && python -m pytest tests/agent/ -v`
Expected: All PASS.

- [ ] **Step 6: Commit**

```bash
git add src/agent/agents/base.py tests/agent/agents/test_base.py
git commit -m "feat: inject ref instructions into system prompt when ContextManager is active"
```

---

### Task 8: Integration test — full ref resolution flow

**Files:**
- Test: `tests/agent/test_context_manager.py` (add integration test class)

- [ ] **Step 1: Write the integration test**

Add to `tests/agent/test_context_manager.py`:

```python
class TestRefResolutionIntegration:
    @pytest.mark.asyncio
    async def test_persist_then_resolve_roundtrip(self, mgr):
        """Full flow: persist large output, then resolve ref back to original data."""
        original_data = {"pages": [{"text": "Document paragraph " * 100}]}
        marker = mgr.persist_large_output("parse_document", original_data)

        # Marker has ref_id
        assert marker["__persisted_output__"] is True
        assert "ref_id" in marker

        # Simulate LLM passing ref_id as tool argument
        ref_id = marker["ref_id"]
        kwargs = {"document": ref_id, "doc_type": "通知"}

        # Resolve
        resolved = mgr.resolve_refs(kwargs)
        assert resolved["document"] == original_data
        assert resolved["doc_type"] == "通知"

    @pytest.mark.asyncio
    async def test_compact_preserves_ref_map(self, mgr, mock_model):
        """After full compaction, ref_map still works."""
        data = {"big": "data " * 200}
        marker = mgr.persist_large_output("search_documents", data)
        ref_id = marker["ref_id"]

        # Create messages exceeding budget to trigger compaction
        msgs = tuple(
            Message(role="user", content="x" * 3000)
            for _ in range(3)
        )
        compacted = await mgr.compact_if_needed(msgs)

        # ref_map should still resolve
        kwargs = {"data": ref_id}
        resolved = mgr.resolve_refs(kwargs)
        assert resolved["data"] == data
```

- [ ] **Step 2: Run integration tests**

Run: `cd /home/lmwl/Documents/docaudit/docaudit-backend/.worktrees/agent-phase0 && python -m pytest tests/agent/test_context_manager.py::TestRefResolutionIntegration -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add tests/agent/test_context_manager.py
git commit -m "test: add integration tests for full ref resolution roundtrip"
```

---

### Task 9: Final validation — run full test suite

- [ ] **Step 1: Run full test suite**

Run: `cd /home/lmwl/Documents/docaudit/docaudit-backend/.worktrees/agent-phase0 && python -m pytest tests/ -v`
Expected: All PASS.

- [ ] **Step 2: Check for any remaining `_PASSTHROUGH_TOOLS` or `passthrough_tools` references**

Run: `cd /home/lmwl/Documents/docaudit/docaudit-backend/.worktrees/agent-phase0 && grep -rn "passthrough\|PASSTHROUGH" src/ tests/`
Expected: No matches (all removed in Task 2).

- [ ] **Step 3: Check for any remaining references to old `persist_large_output` marker without `ref_id`**

Run: `cd /home/lmwl/Documents/docaudit/docaudit-backend/.worktrees/agent-phase0 && grep -rn "__persisted_output__" src/ tests/`
Expected: Only the marker dict construction in `persist_large_output` and test assertions.
