# Tool Output Reference Resolution Design

**Date:** 2026-05-22
**Branch:** feature/agent-phase0-foundation

## Problem

`ContextManager.persist_large_output()` saves large tool outputs to disk and replaces them with marker dicts in the conversation context. However, downstream tools that need the full data (e.g., `audit_format` needs `parse_document`'s output) break when they receive markers instead of data.

Current workaround: a hardcoded `_PASSTHROUGH_TOOLS` set (18 tools) whose outputs are never persisted. This defeats the purpose of Layer 1 — those tools' large outputs blow up the context window.

## Solution

Persist ALL tool outputs and introduce a reference resolution mechanism:

1. Each persisted output gets a semantic ID (e.g., `$ref:parse_document:1`)
2. LLM is guided to use these IDs as tool call argument values
3. System automatically resolves IDs to full data before tool execution

This removes `_PASSTHROUGH_TOOLS` entirely.

---

## Design

### 1. Semantic ID and Persistent Storage

**ID format:** `$ref:<tool_name>:<sequence>`

- `tool_name`: snake_case tool name (same as registered name)
- `sequence`: per-tool incrementing counter (1, 2, 3...)

**Mapping table:** `dict[str, str]` stored in `CompactState` within `ContextManager`. Key = semantic ID, value = disk file path. Lifecycle is one agent run.

**`persist_large_output` changes:**

- Existing serialization/disk-write logic unchanged
- Generate semantic ID, add to mapping table
- Return marker dict with new `ref_id` field:

```python
{
    "__persisted_output__": True,
    "ref_id": "$ref:parse_document:1",
    "file": ".agent_cache/parse_document_1716352800000.json",
    "size_chars": 15000,
    "preview": "前 2000 字符...",
}
```

**`_PASSTHROUGH_TOOLS`:** Removed entirely. All tools go through persistence uniformly.

### 2. Reference Resolution Before Tool Execution

**Entry point:** `ToolRegistry.execute()` gains an optional `context_manager` parameter.

```python
async def execute(self, name: str, context_manager: ContextManager | None = None, **kwargs) -> ToolResult:
    tool = self.get(name)
    if context_manager:
        kwargs = context_manager.resolve_refs(kwargs)
    return await tool.execute(**kwargs)
```

**`ContextManager.resolve_refs(kwargs)` logic:**

- Recursively traverse all values in kwargs (dicts, lists, strings)
- If a string matches pattern `^\$ref:([a-z_]+):(\d+)$`, look up in mapping table
- On hit: load JSON from disk, replace string with parsed data
- On miss: log warning, keep original string (do not abort)

**`agent_loop` change:** Pass `context_manager` to `tool_registry.execute`:

```python
result = await tool_registry.execute(
    tool_call.name, context_manager=context_manager, **tool_call.arguments
)
```

### 3. Prompt Guidance for LLM

`ContextManager.get_ref_instructions() -> str` returns a short instruction block:

```
当工具返回包含 __persisted_output__ 标记的结果时，完整数据已存入磁盘。
如需将此数据作为后续工具的参数传入，请使用 ref_id 的值（如 "$ref:parse_document:1"）
作为参数值。系统会自动加载完整数据替换引用。
```

- Injected into system prompt or task when `ContextManager` is active
- Fixed ~100 chars, does not grow with number of persisted outputs
- Does not list all available ref IDs — LLM reads them from marker dicts in context

### 4. Error Handling and Edge Cases

**Missing ref ID in mapping table:** Keep original string, log warning. Let the tool itself error for debuggability.

**Disk file missing:** Same behavior — warning log, keep original string.

**Circular/nested refs:** Not possible. `$ref:...` is a plain string; resolved result is loaded JSON, never re-parsed.

**Ref as nested value in dict/list:** `resolve_refs` recurses into dict values and list elements at any depth.

```python
{"document": "$ref:parse_document:1", "doc_type": "通知"}
# → {"document": {<full parsed doc>}, "doc_type": "通知"}
```

**Multiple calls to same tool:** Sequence counter increments per tool_name. `$ref:parse_document:1`, `$ref:parse_document:2` point to different files.

**Compact impact:** `micro_compact` and `full_compact` replace/omit old tool result messages, but the `ref_id → filepath` mapping table is independent of message history. LLM can still reference historical outputs after compaction via `ref_id`.

---

## Files to Modify

| File | Change |
|------|--------|
| `src/agent/core/context_manager.py` | Add mapping table, ref ID generation, `resolve_refs()`, `get_ref_instructions()`, remove `_PASSTHROUGH_TOOLS` |
| `src/agent/tools/registry.py` | Add `context_manager` param to `execute()` |
| `src/agent/core/loop.py` | Pass `context_manager` to `tool_registry.execute()` |
| `src/agent/agents/base.py` | Inject ref instructions into prompt when `ContextManager` is active |
| `tests/agent/core/test_context_manager.py` | New test file for ref resolution |
| `tests/agent/test_loop.py` | Update tests for new `execute()` signature |
