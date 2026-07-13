# CacheStore 统一缓存层 + 工具参数 ref_id 自动解析 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 创建 CacheStore 统一缓存层，支持 JSON/纯文本持久化，ToolRegistry 统一持久化工具结果，ref_id 参数类型自适应解析。

**Architecture:** 新建 `cache_store.py` 作为缓存存储唯一入口，从 ContextManager 中提取持久化/ref 管理逻辑。ToolRegistry.execute() 执行后自动调用 CacheStore.persist()，执行前通过 CacheStore.resolve_refs() 解析 $ref 引用。ContextManager 重构为委托 CacheStore。

**Tech Stack:** Python 3.12+, pytest, asyncio, jq

---

## 文件职责

| 文件 | 职责 | 操作 |
|------|------|------|
| `src/agent/core/cache_store.py` | 持久化(JSON/文本)、ref_id 管理、ref 解析、文件读写、模式提取 | **新建** |
| `src/agent/core/context_manager.py` | 三层上下文预算控制(L1/2/3)，持久化/ref 逻辑委托给 CacheStore | **修改** |
| `src/agent/tools/registry.py` | 工具发现与执行，执行后调用 CacheStore 统一持久化，执行前类型自适应解析 | **修改** |
| `src/agent/tools/protocol.py` | 增加 skip_persist, output_content_type 可选属性 | **修改** |
| `src/agent/tools/builtin/read_cached.py` | query 调用时传入 label 和 source 元数据 | **修改** |
| `src/agent/core/loop.py` | 移除 Layer1 重复持久化逻辑（改由 ToolRegistry 统一处理） | **修改** |
| `tests/agent/test_cache_store.py` | CacheStore 单元测试 | **新建** |
| `tests/agent/test_context_manager.py` | 适配重构后的接口 | **修改** |
| `tests/agent/tools/test_read_cached.py` | 扩展 — 验证 source 元数据传递 | **修改** |

---

### Task 1: Create CacheStore class

**Files:**
- Create: `src/agent/core/cache_store.py`

- [ ] **Step 1: Write the failing tests for CacheStore.persist (JSON)**

Create `tests/agent/test_cache_store.py`:

```python
"""Tests for CacheStore — unified cache storage layer."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from src.agent.core.cache_store import CacheStore, PersistResult


class TestCacheStorePersistJson:
    def test_small_data_passes_through(self, tmp_path):
        store = CacheStore(cache_dir=str(tmp_path))
        result = store.persist({"key": "value"}, "test_tool")
        assert result.data == {"key": "value"}
        assert result.persisted is False

    def test_large_data_persisted_with_ref_id(self, tmp_path):
        store = CacheStore(cache_dir=str(tmp_path))
        data = {"text": "x" * 1000}
        result = store.persist(data, "search_documents")
        assert result.persisted is True
        assert isinstance(result.data, dict)
        assert result.data["__persisted_output__"] is True
        assert result.data["ref_id"] == "$ref:search_documents:1"
        assert "file" in result.data

    def test_ref_id_increments_per_tool(self, tmp_path):
        store = CacheStore(cache_dir=str(tmp_path))
        data = {"text": "x" * 1000}
        r1 = store.persist(data, "tool_a")
        r2 = store.persist(data, "tool_a")
        r3 = store.persist(data, "tool_b")
        assert r1.data["ref_id"] == "$ref:tool_a:1"
        assert r2.data["ref_id"] == "$ref:tool_a:2"
        assert r3.data["ref_id"] == "$ref:tool_b:1"

    def test_ref_map_tracks_all(self, tmp_path):
        store = CacheStore(cache_dir=str(tmp_path))
        data = {"text": "x" * 1000}
        r1 = store.persist(data, "tool_a")
        r2 = store.persist(data, "tool_b")
        assert store.ref_map[r1.data["ref_id"]] == r1.data["file"]
        assert store.ref_map[r2.data["ref_id"]] == r2.data["file"]

    def test_none_data_passes_through(self, tmp_path):
        store = CacheStore(cache_dir=str(tmp_path))
        result = store.persist(None, "test_tool")
        assert result.data is None
        assert result.persisted is False

    def test_file_on_disk_matches_data(self, tmp_path):
        store = CacheStore(cache_dir=str(tmp_path))
        data = {"text": "x" * 1000}
        result = store.persist(data, "search_documents")
        filepath = result.data["file"]
        with open(filepath, encoding="utf-8") as f:
            loaded = json.load(f)
        assert loaded == data

    def test_content_type_json_in_marker(self, tmp_path):
        store = CacheStore(cache_dir=str(tmp_path))
        data = {"text": "x" * 1000}
        result = store.persist(data, "tool_a")
        assert result.data["content_type"] == "application/json"

    def test_schema_file_created(self, tmp_path):
        store = CacheStore(cache_dir=str(tmp_path))
        data = {"metadata": {"author": "张三"}, "pages": [{"page_no": 1, "text": "x" * 1000}]}
        result = store.persist(data, "parse_document")
        filepath = result.data["file"]
        schema_path = filepath.replace(".json", ".schema.json")
        assert os.path.exists(schema_path)
        schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
        assert "metadata.author" in schema
        assert "pages[].page_no" in schema

    def test_force_persists_small_data(self, tmp_path):
        store = CacheStore(cache_dir=str(tmp_path))
        data = {"key": "small"}
        result = store.persist(data, "test_tool", force=True)
        assert result.persisted is True
        assert result.data["__persisted_output__"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/agent/test_cache_store.py -v`
Expected: FAIL with ModuleNotFoundError for cache_store

- [ ] **Step 3: Write minimal CacheStore implementation (persist + JSON)**

Create `src/agent/core/cache_store.py`:

```python
"""CacheStore — unified cache storage layer for JSON and plain text."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .schema_utils import extract_field_paths, merge_schema

logger = logging.getLogger(__name__)

_REF_PATTERN = re.compile(r"^\$ref:([a-zA-Z_][a-zA-Z0-9_]*):(\d+)(?::([a-zA-Z_][a-zA-Z0-9_]*))?$")

LARGE_OUTPUT_THRESHOLD = 3_000
MAX_RESOLVE_DEPTH = 32


@dataclass
class PersistResult:
    data: Any
    ref_id: str
    persisted: bool


class CacheStore:
    """统一缓存存储层，支持 JSON 和纯文本格式。"""

    def __init__(
        self,
        cache_dir: str = ".agent_cache",
        large_output_threshold: int = LARGE_OUTPUT_THRESHOLD,
    ) -> None:
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self.large_output_threshold = large_output_threshold
        self.ref_map: dict[str, str] = {}
        self.ref_counters: dict[str, int] = {}
        self.recent_files: list[str] = []
        self._lock = threading.Lock()

    # -- Persist --

    def persist(
        self,
        data: Any,
        tool_name: str,
        *,
        force: bool = False,
        label: str | None = None,
        source_ref_id: str | None = None,
        source_query: str | None = None,
        tool_registry: Any = None,
    ) -> PersistResult:
        """Persist data to disk. Small data passes through, large data gets wrapped."""
        if data is None:
            return PersistResult(data=None, ref_id="", persisted=False)

        content_type, serialized = self._detect_content_type(data)

        if not force and len(serialized) <= self.large_output_threshold:
            return PersistResult(data=data, ref_id="", persisted=False)

        # Generate ref_id: $ref:<tool_name>:<seq>[:<label>]
        ref_id = self._next_ref_id(tool_name, label)

        # Determine file extension
        ext = ".txt" if content_type == "text/plain" else ".json"
        filepath_str = self._write_file(tool_name, ref_id, serialized, ext)

        # Persist schema for JSON data
        if isinstance(data, (dict, list)) and content_type == "application/json":
            self._persist_schema(data, filepath_str, tool_name, tool_registry)

        # Build marker
        preview = serialized[:200]
        if len(serialized) > 200:
            preview += f"\n...[truncated, full output ({len(serialized)} chars) saved to {filepath_str}]"

        marker: dict[str, Any] = {
            "__persisted_output__": True,
            "ref_id": ref_id,
            "file": filepath_str,
            "size_chars": len(serialized),
            "preview": preview,
            "content_type": content_type,
        }
        if label:
            marker["label"] = label
        if source_ref_id or source_query:
            marker["source"] = {}
            if source_ref_id:
                marker["source"]["ref_id"] = source_ref_id
            if source_query:
                marker["source"]["query"] = source_query

        return PersistResult(data=marker, ref_id=ref_id, persisted=True)

    def _next_ref_id(self, tool_name: str, label: str | None) -> str:
        with self._lock:
            seq = self.ref_counters.get(tool_name, 0) + 1
            self.ref_counters[tool_name] = seq
        if label:
            return f"$ref:{tool_name}:{seq}:{label}"
        return f"$ref:{tool_name}:{seq}"

    def _write_file(self, tool_name: str, ref_id: str, serialized: str, ext: str) -> str:
        ts = int(time.time() * 1000)
        safe_name = tool_name.replace("/", "_").replace(" ", "_")
        seq = self.ref_counters.get(tool_name, 0)
        filename = f"{safe_name}_{seq}_{ts}{ext}"
        filepath = self._cache_dir / filename
        filepath.write_text(serialized, encoding="utf-8")
        filepath_str = str(filepath)

        with self._lock:
            self.ref_map[ref_id] = filepath_str
            self.recent_files.append(filepath_str)
            if len(self.recent_files) > 100:
                self.recent_files = self.recent_files[-100:]

        return filepath_str

    def _persist_schema(
        self, data: Any, filepath_str: str, tool_name: str, tool_registry: Any
    ) -> None:
        try:
            actual_paths = extract_field_paths(data)
            base_schema = None
            if tool_registry:
                base_schema = tool_registry.get_output_schema(tool_name)
            merged = merge_schema(base_schema, actual_paths)
            schema_path = filepath_str.replace(".json", ".schema.json")
            Path(schema_path).write_text(json.dumps(merged, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    # -- Content type detection --

    def _detect_content_type(self, data: Any) -> tuple[str, str]:
        """Returns (content_type, serialized_string)."""
        if isinstance(data, (dict, list)):
            return "application/json", json.dumps(data, ensure_ascii=False)
        if isinstance(data, str):
            # Try to detect if it's already JSON content
            try:
                parsed = json.loads(data)
                if isinstance(parsed, (dict, list)):
                    return "application/json", data
            except (json.JSONDecodeError, ValueError):
                pass
            return "text/plain", data
        # Other types: convert to string
        return "text/plain", str(data)

    # -- Resolve refs --

    def resolve_refs(
        self,
        kwargs: dict[str, Any],
        param_schemas: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Recursively resolve $ref strings in kwargs, adapting to parameter type."""
        return self._resolve_value(kwargs, param_schemas, _depth=0)

    def _resolve_value(
        self, value: Any, param_schemas: dict[str, Any] | None, _depth: int
    ) -> Any:
        if _depth > MAX_RESOLVE_DEPTH:
            logger.warning("Max resolve depth exceeded; returning value as-is")
            return value
        if isinstance(value, str):
            match = _REF_PATTERN.match(value)
            if match:
                return self._load_and_adapt(value, param_schemas)
            return value
        if isinstance(value, dict):
            if value.get("__persisted_output__") is True:
                filepath = value.get("file")
                if filepath:
                    ref_id = value.get("ref_id", filepath)
                    loaded = self._load_ref(ref_id, filepath)
                    if loaded is not ref_id:
                        return loaded
                return value
            return {k: self._resolve_value(v, param_schemas, _depth + 1) for k, v in value.items()}
        if isinstance(value, list):
            return [self._resolve_value(v, param_schemas, _depth + 1) for v in value]
        return value

    def _load_and_adapt(self, ref_id: str, param_schemas: dict[str, Any] | None) -> Any:
        """Load a ref and adapt its type based on param_schemas if available."""
        filepath = self.ref_map.get(ref_id)
        if filepath is None:
            logger.warning("Unknown ref_id: %s", ref_id)
            return ref_id

        raw = self._load_ref(ref_id, filepath)
        if raw is ref_id:
            return ref_id

        # If no schema hint, return raw data as-is
        if param_schemas is None:
            return raw

        expected_type = param_schemas.get("type", "")
        return self._adapt_value(raw, expected_type)

    def _adapt_value(self, raw: Any, expected_type: str) -> Any:
        """Adapt loaded data to match the expected parameter type."""
        if expected_type == "string":
            if isinstance(raw, str):
                return raw
            return json.dumps(raw, ensure_ascii=False)
        if expected_type in ("object", "array"):
            if isinstance(raw, str):
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning("Expected %s but ref content is non-JSON text", expected_type)
                    return raw
            return raw
        if expected_type == "number":
            if isinstance(raw, str):
                try:
                    return float(raw)
                except ValueError:
                    return raw
            return raw
        if expected_type == "integer":
            if isinstance(raw, str):
                try:
                    return int(raw)
                except ValueError:
                    return raw
            return raw
        if expected_type == "boolean":
            if isinstance(raw, str):
                return raw.lower() in ("true", "1", "yes")
            return bool(raw)
        return raw

    def _load_ref(self, ref_id: str, filepath: str) -> Any:
        """Load data from disk. Returns original ref_id string on failure."""
        try:
            content = Path(filepath).read_text(encoding="utf-8")
        except (FileNotFoundError, OSError) as exc:
            logger.warning("Failed to load ref %s from %s: %s", ref_id, filepath, exc)
            return ref_id

        # Detect content type from file extension
        if filepath.endswith(".txt"):
            return content

        # JSON file
        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            logger.warning("Failed to parse JSON for ref %s: %s", ref_id, exc)
            return content

    def load(self, ref_id: str) -> Any:
        """Public load method — returns raw data or ref_id string on failure."""
        filepath = self.ref_map.get(ref_id)
        if filepath is None:
            return ref_id
        return self._load_ref(ref_id, filepath)

    def exists(self, ref_id: str) -> bool:
        return ref_id in self.ref_map

    def get_info(self, ref_id: str) -> dict | None:
        filepath = self.ref_map.get(ref_id)
        if filepath is None:
            return None
        p = Path(filepath)
        info: dict[str, Any] = {"file": filepath, "ref_id": ref_id}
        try:
            info["size"] = p.stat().st_size
        except OSError:
            pass
        info["content_type"] = "text/plain" if p.suffix == ".txt" else "application/json"
        return info
```

- [ ] **Step 4: Run tests to verify persist tests pass**

Run: `python -m pytest tests/agent/test_cache_store.py -v -k "PersistJson"`
Expected: all PersistJson tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/agent/core/cache_store.py tests/agent/test_cache_store.py
git commit -m "feat: add CacheStore with JSON persist and ref management"
```

---

### Task 2: Add plain text persist and resolve_refs to CacheStore

**Files:**
- Modify: `src/agent/core/cache_store.py`
- Modify: `tests/agent/test_cache_store.py`

- [ ] **Step 1: Write failing tests for text persist and resolve_refs**

Append to `tests/agent/test_cache_store.py`:

```python
class TestCacheStorePersistText:
    def test_plain_text_stored_as_txt(self, tmp_path):
        store = CacheStore(cache_dir=str(tmp_path))
        data = "command output\nline2\nline3 " + "x" * 3000
        result = store.persist(data, "run_shell")
        assert result.persisted is True
        assert result.data["content_type"] == "text/plain"
        assert result.data["file"].endswith(".txt")

    def test_string_of_json_dict_stored_as_json(self, tmp_path):
        store = CacheStore(cache_dir=str(tmp_path))
        data = json.dumps({"result": "x" * 3000})
        result = store.persist(data, "tool_a")
        assert result.data["content_type"] == "application/json"

    def test_non_json_non_dict_serialized_as_text(self, tmp_path):
        store = CacheStore(cache_dir=str(tmp_path))
        data = 42
        result = store.persist(data, "tool_a", force=True)
        assert result.data["content_type"] == "text/plain"


class TestCacheStoreResolveRefs:
    def test_resolves_ref_string_to_disk_data(self, tmp_path):
        store = CacheStore(cache_dir=str(tmp_path))
        data = {"pages": [{"text": "x" * 1000}]}
        store.persist(data, "parse_document")

        kwargs = {"document": "$ref:parse_document:1", "doc_type": "通知"}
        resolved = store.resolve_refs(kwargs)
        assert resolved["document"] == data
        assert resolved["doc_type"] == "通知"

    def test_no_refs_returns_unchanged(self, tmp_path):
        store = CacheStore(cache_dir=str(tmp_path))
        kwargs = {"text": "hello", "count": 5}
        resolved = store.resolve_refs(kwargs)
        assert resolved == kwargs

    def test_unknown_ref_keeps_original(self, tmp_path):
        store = CacheStore(cache_dir=str(tmp_path))
        kwargs = {"document": "$ref:nonexistent:99"}
        resolved = store.resolve_refs(kwargs)
        assert resolved["document"] == "$ref:nonexistent:99"

    def test_resolves_nested_in_list(self, tmp_path):
        store = CacheStore(cache_dir=str(tmp_path))
        data = {"result": "x" * 1000}
        store.persist(data, "audit_format")
        kwargs = {"items": ["$ref:audit_format:1", "plain"]}
        resolved = store.resolve_refs(kwargs)
        assert resolved["items"][0] == data
        assert resolved["items"][1] == "plain"

    def test_resolves_nested_in_dict(self, tmp_path):
        store = CacheStore(cache_dir=str(tmp_path))
        data = {"pages": "x" * 1000}
        store.persist(data, "parse_document")
        kwargs = {"outer": {"doc": "$ref:parse_document:1"}}
        resolved = store.resolve_refs(kwargs)
        assert resolved["outer"]["doc"] == data

    def test_resolves_marker_dict(self, tmp_path):
        store = CacheStore(cache_dir=str(tmp_path))
        data = {"pages": [{"text": "x" * 1000}]}
        marker = store.persist(data, "parse_document")
        kwargs = {"document": marker.data, "doc_type": "通知"}
        resolved = store.resolve_refs(kwargs)
        assert resolved["document"] == data

    def test_file_missing_falls_back(self, tmp_path):
        store = CacheStore(cache_dir=str(tmp_path))
        data = {"text": "x" * 1000}
        store.persist(data, "search_documents")
        ref_id = "$ref:search_documents:1"
        filepath = store.ref_map[ref_id]
        os.remove(filepath)
        kwargs = {"data": ref_id}
        resolved = store.resolve_refs(kwargs)
        assert resolved["data"] == ref_id


class TestCacheStoreTypeAdaptive:
    def test_string_param_returns_text(self, tmp_path):
        store = CacheStore(cache_dir=str(tmp_path))
        data = "plain text output " + "x" * 3000
        store.persist(data, "run_shell")
        kwargs = {"content": "$ref:run_shell:1"}
        resolved = store.resolve_refs(kwargs, param_schemas={"type": "string"})
        assert isinstance(resolved["content"], str)
        assert "plain text output" in resolved["content"]

    def test_object_param_returns_parsed_json(self, tmp_path):
        store = CacheStore(cache_dir=str(tmp_path))
        data = {"key": "value", "items": [1, 2, 3]}
        store.persist(data, "tool_a")
        kwargs = {"params": "$ref:tool_a:1"}
        resolved = store.resolve_refs(kwargs, param_schemas={"type": "object"})
        assert isinstance(resolved["params"], dict)
        assert resolved["params"] == data

    def test_text_content_object_param_falls_back(self, tmp_path):
        store = CacheStore(cache_dir=str(tmp_path))
        data = "not json content " + "x" * 3000
        store.persist(data, "run_shell")
        kwargs = {"params": "$ref:run_shell:1"}
        resolved = store.resolve_refs(kwargs, param_schemas={"type": "object"})
        assert isinstance(resolved["params"], str)

    def test_no_schema_returns_raw(self, tmp_path):
        store = CacheStore(cache_dir=str(tmp_path))
        data = {"key": "value"}
        store.persist(data, "tool_a", force=True)
        kwargs = {"params": "$ref:tool_a:1"}
        resolved = store.resolve_refs(kwargs)  # no param_schemas
        assert resolved["params"] == data


class TestCacheStoreLabel:
    def test_label_in_ref_id(self, tmp_path):
        store = CacheStore(cache_dir=str(tmp_path))
        data = {"text": "x" * 1000}
        result = store.persist(data, "read_cached_output", label="doc_params")
        assert result.data["ref_id"] == "$ref:read_cached_output:1:doc_params"
        assert result.data["label"] == "doc_params"

    def test_label_in_marker(self, tmp_path):
        store = CacheStore(cache_dir=str(tmp_path))
        data = {"text": "x" * 1000}
        result = store.persist(data, "tool_a", label="my_label")
        assert result.data["label"] == "my_label"


class TestCacheStoreSourceMetadata:
    def test_source_in_marker(self, tmp_path):
        store = CacheStore(cache_dir=str(tmp_path))
        data = {"id": "D001", "text": "x" * 1000}
        result = store.persist(
            data, "read_cached_output",
            source_ref_id="$ref:parse_document:1",
            source_query="{id: .metadata.id}",
        )
        assert result.data["source"]["ref_id"] == "$ref:parse_document:1"
        assert result.data["source"]["query"] == "{id: .metadata.id}"

    def test_source_partial(self, tmp_path):
        store = CacheStore(cache_dir=str(tmp_path))
        data = {"text": "x" * 1000}
        result = store.persist(data, "tool_a", source_ref_id="$ref:parse_document:1")
        assert result.data["source"]["ref_id"] == "$ref:parse_document:1"
        assert "query" not in result.data["source"]


class TestCacheStoreLoad:
    def test_load_returns_parsed_data(self, tmp_path):
        store = CacheStore(cache_dir=str(tmp_path))
        data = {"key": "value", "list": [1, 2, 3]}
        store.persist(data, "tool_a", force=True)
        loaded = store.load("$ref:tool_a:1")
        assert loaded == data

    def test_load_text_file(self, tmp_path):
        store = CacheStore(cache_dir=str(tmp_path))
        data = "plain text " + "x" * 3000
        store.persist(data, "run_shell")
        loaded = store.load("$ref:run_shell:1")
        assert loaded == data

    def test_load_unknown_returns_string(self, tmp_path):
        store = CacheStore(cache_dir=str(tmp_path))
        assert store.load("$ref:nonexistent:99") == "$ref:nonexistent:99"

    def test_exists_and_get_info(self, tmp_path):
        store = CacheStore(cache_dir=str(tmp_path))
        data = {"text": "x" * 1000}
        store.persist(data, "tool_a")
        assert store.exists("$ref:tool_a:1") is True
        assert store.exists("$ref:nonexistent:1") is False
        info = store.get_info("$ref:tool_a:1")
        assert info is not None
        assert "file" in info
        assert "size" in info
```

- [ ] **Step 2: Run tests to verify failures**

Run: `python -m pytest tests/agent/test_cache_store.py -v -k "PersistText or ResolveRefs or TypeAdaptive or Label or SourceMetadata or Load"`
Expected: FAIL (some tests may already pass from Task 1 implementation)

- [ ] **Step 3: Ensure all new tests pass**

Run: `python -m pytest tests/agent/test_cache_store.py -v`
Expected: all tests PASS

- [ ] **Step 4: Commit**

```bash
git add tests/agent/test_cache_store.py
git commit -m "test: add text persist, resolve_refs, type-adaptive, label, source tests for CacheStore"
```

---

### Task 3: Refactor ContextManager to delegate to CacheStore

**Files:**
- Modify: `src/agent/core/context_manager.py`
- Modify: `tests/agent/test_context_manager.py`

- [ ] **Step 1: Update ContextManager to accept and use CacheStore**

In `src/agent/core/context_manager.py`, modify `ContextManager.__init__`:

```python
# In __init__, add cache_store parameter:
def __init__(
    self,
    model: ModelClient,
    cache_dir: str = ".agent_cache",
    max_context_chars: int = MAX_CONTEXT_CHARS,
    large_output_threshold: int = LARGE_OUTPUT_THRESHOLD,
    recent_tool_results: int = RECENT_TOOL_RESULTS,
    tool_registry: Any = None,
    cache_store: Any = None,  # NEW: optional external CacheStore
) -> None:
    self._model = model
    self.max_context_chars = max_context_chars
    self.large_output_threshold = large_output_threshold
    self.recent_tool_results = recent_tool_results
    self.state = CompactState()
    self._state_lock = threading.Lock()
    self._tool_registry = tool_registry

    # Use provided CacheStore or create one
    if cache_store is not None:
        self._cache = cache_store
    else:
        from .cache_store import CacheStore as _CacheStore
        self._cache = _CacheStore(
            cache_dir=cache_dir,
            large_output_threshold=large_output_threshold,
        )
```

- [ ] **Step 2: Replace _persist_data with delegation to CacheStore**

Replace the `_persist_data` method:

```python
def _persist_data(self, tool_name: str, serialized: str) -> tuple[str, str]:
    """Persist serialized data to disk and return (ref_id, filepath).

    Delegates to CacheStore for storage but returns only (ref_id, filepath)
    for backward compatibility with internal callers.
    """
    result = self._cache.persist(
        json.loads(serialized),
        tool_name,
        force=True,
        tool_registry=self._tool_registry,
    )
    return result.ref_id, self._cache.ref_map[result.ref_id]
```

- [ ] **Step 3: Replace persist_large_output to delegate to CacheStore**

Replace `persist_large_output`:

```python
def persist_large_output(
    self, tool_name: str, data: object, force: bool = False
) -> object:
    if data is None:
        return data

    try:
        serialized = json.dumps(data, ensure_ascii=False)
    except (TypeError, ValueError):
        return data

    if not force and len(serialized) <= self.large_output_threshold:
        return data

    result = self._cache.persist(
        data, tool_name,
        force=force,
        tool_registry=self._tool_registry,
    )
    return result.data
```

- [ ] **Step 4: Replace _load_ref to delegate to CacheStore**

Replace `_load_ref`:

```python
def _load_ref(self, ref_id: str, filepath: str) -> Any:
    return self._cache._load_ref(ref_id, filepath)
```

- [ ] **Step 5: Replace ref_map access in ContextManager**

In `_persist_data` and elsewhere, use `self._cache.ref_map` instead of `self.state.ref_map`. Update `CompactState` to remove `ref_map` and `ref_counters` (they live in CacheStore now).

Update `CompactState`:

```python
@dataclass
class CompactState:
    has_compacted: bool = False
    last_summary: str | None = None
    recent_files: list[str] = field(default_factory=list)
    compact_count: int = 0
    compaction_disabled: bool = False
    # ref_map and ref_counters moved to CacheStore
```

- [ ] **Step 6: Update all internal ref_map references**

In methods that reference `self.state.ref_map`:
- `resolve_refs` → use `self._cache.ref_map`
- `_load_ref` → already delegated
- `micro_compact` → update `_extract_ref_id` and `_persist_tool_message`
- `_persist_tool_message` → use `self._cache.persist()` instead of `self._persist_data()`

In `_persist_tool_message`:

```python
def _persist_tool_message(self, msg: Message, tool_name: str) -> str | None:
    if not msg.content:
        return None
    try:
        payload = json.loads(msg.content)
        data = payload.get("data")
        if data is None:
            return None
    except (json.JSONDecodeError, TypeError):
        return None

    result = self._cache.persist(data, tool_name, force=True)
    logger.debug(
        "Micro-compact persisted: %s → %s", tool_name, result.ref_id,
    )
    return result.ref_id
```

- [ ] **Step 7: Run existing ContextManager tests**

Run: `python -m pytest tests/agent/test_context_manager.py -v`
Expected: all tests PASS (or fix any that break due to refactoring)

- [ ] **Step 8: Update test fixtures if needed**

In `tests/agent/test_context_manager.py`, update the `mgr` fixture if needed to pass `cache_store`. Since CacheStore is auto-created by default, existing tests should work without changes. Verify:

Run: `python -m pytest tests/agent/test_context_manager.py -v`
Expected: all tests PASS

- [ ] **Step 9: Commit**

```bash
git add src/agent/core/context_manager.py tests/agent/test_context_manager.py
git commit -m "refactor: delegate persist/ref logic from ContextManager to CacheStore"
```

---

### Task 4: Update ToolProtocol with new attributes

**Files:**
- Modify: `src/agent/tools/protocol.py`

- [ ] **Step 1: Add skip_persist and output_content_type to ToolProtocol**

In `src/agent/tools/protocol.py`, update `ToolProtocol`:

```python
@runtime_checkable
class ToolProtocol(Protocol):
    """Agent 可调用的最小能力单元。"""

    name: str
    description: str
    parameters: dict  # JSON Schema
    output_schema: dict | None  # 可选：工具输出的 JSON Schema
    skip_persist: bool  # 新增：跳过自动持久化，默认 False
    output_content_type: str | None  # 新增：显式声明输出内容类型

    async def execute(self, **kwargs: Any) -> ToolResult:
        """Execute the tool with validated parameters."""
        ...
```

- [ ] **Step 2: Verify existing tools still work**

Run: `python -m pytest tests/agent/tools/test_read_cached.py -v`
Expected: all tests PASS (ReadCachedOutputTool class attrs default to False/None which is compatible)

- [ ] **Step 3: Commit**

```bash
git add src/agent/tools/protocol.py
git commit -m "feat: add skip_persist and output_content_type to ToolProtocol"
```

---

### Task 5: Update ToolRegistry.execute() for unified persist + type-adaptive resolve

**Files:**
- Modify: `src/agent/tools/registry.py`

- [ ] **Step 1: Write failing tests for ToolRegistry integration**

Append to existing `tests/agent/test_registry.py` (uses fixtures from `tests/agent/conftest.py`):

```python
"""Tests for ToolRegistry with CacheStore integration."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.agent.tools.protocol import ToolProtocol, ToolResult
from src.agent.tools.registry import ToolRegistry
from src.agent.core.cache_store import CacheStore


class FakeTool:
    """A simple tool for testing."""
    name = "fake_tool"
    description = "A fake tool for testing"
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "data": {"type": "object", "description": "Some data"},
        },
        "required": ["data"],
    }

    async def execute(self, data: Any = None, context_manager: Any = None, **kwargs) -> ToolResult:
        return ToolResult(success=True, data={"received": data})


class FakeStringParamTool:
    """Tool that accepts a string parameter."""
    name = "string_tool"
    description = "A tool that takes string input"
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "Text content"},
        },
        "required": ["content"],
    }

    async def execute(self, content: str = "", context_manager: Any = None, **kwargs) -> ToolResult:
        return ToolResult(success=True, data={"length": len(content)})


class FakeSkipPersistTool:
    """Tool that opts out of auto-persist."""
    name = "skip_tool"
    description = "A tool that skips persistence"
    parameters: dict[str, Any] = {"type": "object", "properties": {}}
    skip_persist = True

    async def execute(self, context_manager: Any = None, **kwargs) -> ToolResult:
        return ToolResult(success=True, data={"marker": "__persisted_output__"})


class TestToolRegistryPersist:
    @pytest.mark.asyncio
    async def test_execute_persists_large_result(self, tmp_path):
        store = CacheStore(cache_dir=str(tmp_path))
        registry = ToolRegistry()
        registry.register(FakeTool())

        data = {"text": "x" * 5000}
        result = await registry.execute(
            "fake_tool", cache_store=store, data=data,
        )
        assert result.success
        # Result data should be wrapped in __persisted_output__ marker
        assert isinstance(result.data, dict)
        assert result.data.get("__persisted_output__") is True

    @pytest.mark.asyncio
    async def test_execute_skips_persist_for_small_result(self, tmp_path):
        store = CacheStore(cache_dir=str(tmp_path))
        registry = ToolRegistry()
        registry.register(FakeTool())

        data = {"text": "small"}
        result = await registry.execute(
            "fake_tool", cache_store=store, data=data,
        )
        assert result.success
        assert result.data == {"received": data}  # Not wrapped

    @pytest.mark.asyncio
    async def test_execute_skip_persist_tool(self, tmp_path):
        store = CacheStore(cache_dir=str(tmp_path))
        registry = ToolRegistry()
        registry.register(FakeSkipPersistTool())

        result = await registry.execute("skip_tool", cache_store=store)
        assert result.success
        # Should not wrap in another __persisted_output__
        assert result.data == {"marker": "__persisted_output__"}


class TestToolRegistryResolveRefs:
    @pytest.mark.asyncio
    async def test_resolves_ref_before_execute(self, tmp_path):
        store = CacheStore(cache_dir=str(tmp_path))
        registry = ToolRegistry()
        registry.register(FakeTool())

        # Persist some data first
        data = {"text": "x" * 5000}
        store.persist(data, "parse_document")

        # Call with $ref
        result = await registry.execute(
            "fake_tool", cache_store=store,
            data="$ref:parse_document:1",
        )
        assert result.success
        assert result.data["received"] == data

    @pytest.mark.asyncio
    async def test_resolves_text_ref_for_string_param(self, tmp_path):
        store = CacheStore(cache_dir=str(tmp_path))
        registry = ToolRegistry()
        registry.register(FakeStringParamTool())

        text_data = "plain text content " + "x" * 5000
        store.persist(text_data, "run_shell")

        result = await registry.execute(
            "string_tool", cache_store=store,
            content="$ref:run_shell:1",
        )
        assert result.success
        assert result.data["length"] == len(text_data)

    @pytest.mark.asyncio
    async def test_unknown_ref_kept_as_string(self, tmp_path):
        store = CacheStore(cache_dir=str(tmp_path))
        registry = ToolRegistry()
        registry.register(FakeTool())

        result = await registry.execute(
            "fake_tool", cache_store=store,
            data="$ref:nonexistent:99",
        )
        assert result.success
        assert result.data["received"] == "$ref:nonexistent:99"
```

- [ ] **Step 2: Run tests to verify failures**

Run: `python -m pytest tests/agent/test_registry.py -v`
Expected: FAIL (ToolRegistry.execute doesn't yet accept cache_store)

- [ ] **Step 3: Update ToolRegistry.execute()**

In `src/agent/tools/registry.py`:

```python
async def execute(
    self,
    name: str,
    context_manager: Any | None = None,
    cache_store: Any | None = None,
    **kwargs: Any,
) -> ToolResult:
    """Execute a tool by name with the given arguments.

    If cache_store is provided:
      - Resolve $ref references in kwargs before execution (type-adaptive).
      - Persist successful results after execution.
    """
    tool = self.get(name)

    # Resolve refs before execution (type-adaptive)
    if cache_store is not None:
        # Pass param schemas for type-adaptive resolution
        param_props = tool.parameters.get("properties", {})
        kwargs = cache_store.resolve_refs(kwargs, param_props)

    # Fallback to context_manager.resolve_refs for backward compat
    if context_manager is not None:
        kwargs = context_manager.resolve_refs(kwargs)

    result = await tool.execute(context_manager=context_manager, **kwargs)

    # Persist successful results (unless tool opts out)
    skip = getattr(tool, "skip_persist", False)
    if (
        cache_store is not None
        and result.success
        and result.data is not None
        and not skip
    ):
        # Don't double-wrap: if result.data is already a marker, skip
        if isinstance(result.data, dict) and result.data.get("__persisted_output__"):
            pass
        else:
            persist_result = cache_store.persist(
                result.data, tool.name,
                tool_registry=self,
            )
            result = ToolResult(
                success=result.success,
                data=persist_result.data,
                error=result.error,
                metadata=result.metadata,
            )

    return result
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/agent/test_registry.py -v`
Expected: all tests PASS

- [ ] **Step 5: Ensure existing tests still pass**

Run: `python -m pytest tests/agent/ -v`
Expected: existing tests unaffected

- [ ] **Step 6: Commit**

```bash
git add src/agent/tools/registry.py tests/agent/test_registry.py
git commit -m "feat: add unified persist and type-adaptive ref resolution to ToolRegistry.execute"
```

---

### Task 6: Update agent loop to remove duplicate persist logic

**Files:**
- Modify: `src/agent/core/loop.py`

- [ ] **Step 1: Remove Layer 1 persist from agent_loop**

In `src/agent/core/loop.py`, the Layer 1 persist block (lines 177-195) should be simplified since ToolRegistry.execute() now handles it. The loop still calls `context_manager.persist_large_output` but we need the ToolRegistry to use CacheStore instead.

Update the tool execution block to pass cache_store to registry:

```python
# In agent_loop, replace the tool execution loop (around line 166-202):
results: list[ToolResult] = []
for tool_call in current_state.tool_calls:
    try:
        # Pass cache_store from context_manager for unified persist+resolve
        cache_store = getattr(context_manager, '_cache', None) if context_manager else None
        result = await tool_registry.execute(
            tool_call.name,
            context_manager=context_manager,
            cache_store=cache_store,
            **tool_call.arguments,
        )
    except Exception as exc:
        logger.exception("Tool %s failed", tool_call.name)
        result = ToolResult(success=False, error=str(exc))

    if on_tool_result:
        summary = _tool_result_summary(result)
        await on_tool_result(tool_call.name, summary)

    results.append(result)
```

Remove the old Layer 1 persist block (lines 177-195 in the original) since ToolRegistry.execute() now handles it.

- [ ] **Step 2: Run tests to verify nothing breaks**

Run: `python -m pytest tests/agent/ -v`
Expected: all tests PASS

- [ ] **Step 3: Commit**

```bash
git add src/agent/core/loop.py
git commit -m "refactor: delegate tool result persistence from agent_loop to ToolRegistry.execute"
```

---

### Task 7: Update read_cached_output to pass source metadata

**Files:**
- Modify: `src/agent/tools/builtin/read_cached.py`

- [ ] **Step 1: Update read_cached_output to accept label and pass source metadata**

In `src/agent/tools/builtin/read_cached.py`, the tool's `execute` method should accept optional `label` parameter and pass source metadata to the cache_store when it persists. However, since persistence is now handled by ToolRegistry, the tool itself doesn't need to change — but it does need to provide source info through its result.

Actually, since persistence happens in ToolRegistry.execute() after the tool returns, the source metadata needs to be passed via the ToolResult. Update the ToolResult metadata or a special convention.

Simpler approach: `read_cached_output` can include source tracking in its `metadata` field, and `ToolRegistry.execute()` can read metadata from the result to pass to `cache_store.persist()`.

Update `read_cached.py` to include `source` in result metadata:

```python
async def execute(
    self, ref_id: Any, context_manager: Any = None, query: str | None = None,
    label: str | None = None, **kwargs: Any
) -> ToolResult:
    # ... existing logic ...

    # In _query_mode, add source metadata:
    def _query_mode(self, filepath: str, ref_id: str, query: str) -> ToolResult:
        # ... existing logic ...
        result = self._run_jq(data, query)
        if not result.success:
            return result
        result.metadata["ref_id"] = ref_id
        result.metadata["source_ref_id"] = ref_id    # NEW
        result.metadata["source_query"] = query       # NEW
        return result

    # In _schema_mode, no source (no query):
    def _schema_mode(self, filepath: str, ref_id: str) -> ToolResult:
        # ... existing logic ...
        return ToolResult(
            success=True,
            data={...},
            metadata={"ref_id": ref_id},  # No source since no query
        )
```

And update `ToolRegistry.execute()` to pass source metadata from ToolResult.metadata:

```python
# In ToolRegistry.execute, when persisting:
persist_result = cache_store.persist(
    result.data, tool.name,
    tool_registry=self,
    source_ref_id=result.metadata.get("source_ref_id"),
    source_query=result.metadata.get("source_query"),
)
```

Also update tool parameters to include `label`:

```python
parameters: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ref_id": {...},
        "query": {...},
        "label": {
            "type": "string",
            "description": "可选的语义标签，用于生成可读的 ref_id。如 'doc_params'。",
        },
    },
    "required": ["ref_id"],
}
```

- [ ] **Step 2: Run read_cached tests**

Run: `python -m pytest tests/agent/tools/test_read_cached.py -v`
Expected: all tests PASS

- [ ] **Step 3: Write a test for source metadata in read_cached**

Append to `tests/agent/tools/test_read_cached.py`:

```python
class TestReadCachedSourceMetadata:
    def test_query_result_includes_source_metadata(self, tmp_path):
        import asyncio

        cache_path = tmp_path / "test_cache.json"
        data = {"doc_id": "D001", "text": "hello"}
        cache_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

        tool = ReadCachedOutputTool()
        cm = FakeContextManager()
        cm.state.ref_map["$ref:parse_document:1"] = str(cache_path)

        result = asyncio.run(
            tool.execute(
                ref_id="$ref:parse_document:1",
                query=".doc_id",
                context_manager=cm,
            )
        )

        assert result.success
        assert result.metadata["source_ref_id"] == "$ref:parse_document:1"
        assert result.metadata["source_query"] == ".doc_id"
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/agent/tools/test_read_cached.py::TestReadCachedSourceMetadata -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/agent/tools/builtin/read_cached.py tests/agent/tools/test_read_cached.py src/agent/tools/registry.py
git commit -m "feat: pass source metadata from read_cached_output to CacheStore persist"
```

---

### Task 8: End-to-end integration tests

**Files:**
- Modify: `tests/agent/test_cache_store.py` (or new file `tests/agent/test_cache_integration.py`)

- [ ] **Step 1: Write E2E test for full flow**

Append to `tests/agent/test_cache_store.py`:

```python
class TestCacheStoreEndToEnd:
    def test_persist_read_query_persist_resolve_chain(self, tmp_path):
        """Full chain: persist doc → read schema → jq query → persist query result → resolve ref in downstream tool."""
        import asyncio

        from src.agent.tools.builtin.read_cached import ReadCachedOutputTool
        from src.agent.tools.registry import ToolRegistry

        store = CacheStore(cache_dir=str(tmp_path))

        # Step 1: Persist large document data
        doc_data = {
            "metadata": {"doc_id": "D001", "author": "张三"},
            "pages": [
                {"page_no": 1, "header": {"text": "第一章"}, "content": "Hello " + "world " * 500},
                {"page_no": 2, "header": {"text": "第二章"}, "content": "More " + "text " * 500},
            ],
        }
        persist_result = store.persist(doc_data, "parse_document")
        ref_id = persist_result.ref_id

        # Step 2: ReadCachedOutputTool reads schema (no query)
        read_tool = ReadCachedOutputTool()
        cm = FakeCM(store)
        schema_result = asyncio.run(
            read_tool.execute(ref_id=ref_id, context_manager=cm)
        )
        assert schema_result.success
        assert "schema" in schema_result.data

        # Step 3: ReadCachedOutputTool executes jq query
        query_result = asyncio.run(
            read_tool.execute(
                ref_id=ref_id,
                query="{id: .metadata.doc_id, headers: [.pages[].header.text]}",
                context_manager=cm,
            )
        )
        assert query_result.success
        assert query_result.data == {"id": "D001", "headers": ["第一章", "第二章"]}

        # Step 4: Query result is persisted by ToolRegistry
        # Simulate what ToolRegistry does
        query_persist = store.persist(
            query_result.data, "read_cached_output",
            source_ref_id=ref_id,
            source_query="{id: .metadata.doc_id, headers: [.pages[].header.text]}",
        )
        # If small data, it won't be wrapped. Force persist for testing.
        if not query_persist.persisted:
            query_persist = store.persist(
                query_result.data, "read_cached_output", force=True,
                source_ref_id=ref_id,
                source_query="{id: .metadata.doc_id, headers: [.pages[].header.text]}",
            )

        query_ref_id = query_persist.ref_id

        # Step 5: Downstream tool resolves query result via ref_id
        kwargs = {"params": query_ref_id}
        resolved = store.resolve_refs(kwargs, {"type": "object"})
        assert resolved["params"] == {"id": "D001", "headers": ["第一章", "第二章"]}

    def test_plain_text_end_to_end(self, tmp_path):
        """Shell output → persist as text → resolve as string param."""
        store = CacheStore(cache_dir=str(tmp_path))
        shell_output = "file1.py\ndir/file2.py\n" + "x" * 5000
        persist_result = store.persist(shell_output, "run_shell")
        ref_id = persist_result.ref_id

        kwargs = {"content": ref_id}
        resolved = store.resolve_refs(kwargs, {"type": "string"})
        assert resolved["content"] == shell_output
        assert isinstance(resolved["content"], str)


class FakeCM:
    """Minimal fake context manager for read_cached_output."""
    def __init__(self, store):
        class State:
            ref_map = store.ref_map
        self.state = State()
```

- [ ] **Step 2: Run E2E tests**

Run: `python -m pytest tests/agent/test_cache_store.py::TestCacheStoreEndToEnd -v`
Expected: all PASS

- [ ] **Step 3: Commit**

```bash
git add tests/agent/test_cache_store.py
git commit -m "test: add end-to-end integration tests for CacheStore full flow"
```

---

### Task 9: Final verification and cleanup

- [ ] **Step 1: Run full test suite**

```bash
python -m pytest tests/agent/ -v
```
Expected: all tests PASS

- [ ] **Step 2: Check for any import errors or type issues**

```bash
python -c "from src.agent.core.cache_store import CacheStore; print('OK')"
python -c "from src.agent.core.context_manager import ContextManager; print('OK')"
python -c "from src.agent.tools.registry import ToolRegistry; print('OK')"
```

- [ ] **Step 3: Run existing read_cached regression tests**

```bash
python -m pytest tests/agent/test_regression.py -v
```
Expected: PASS (if this file exists)

- [ ] **Step 4: Commit any final fixes**

```bash
git add -A
git commit -m "chore: final cleanup and fixes for CacheStore integration"
```
```

- [ ] **Step 5: Verify git status is clean**

```bash
git status
```
Expected: clean working tree
```
