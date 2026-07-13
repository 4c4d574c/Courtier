# Search Tool Improvement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace raw ES Query DSL interface with LLM-friendly semantic parameters and clean response formatting.

**Architecture:** Single-file change to `src/agent/skills/search/tool.py`. Extract three pure functions — `_parse_query` (quote detection), `_build_es_query` (ES query construction), `_clean_response` (response formatting) — then update the tool class to use them. Old `query_body` parameter removed.

**Tech Stack:** Python 3.13, Elasticsearch via `src.es.client.search_chunks`, pytest

---

### Task 1: Write query builder unit tests

**Files:**
- Rewrite: `tests/agent/tools/test_search.py`

- [ ] **Step 1: Write `_parse_query` tests**

Replace the file content:

```python
"""Tests for SearchDocumentsTool — query builder, response cleaner, and execution."""

from __future__ import annotations

import pytest

from src.agent.skills.search.tool import (
    SearchDocumentsTool,
    _parse_query,
    _build_es_query,
    _clean_response,
)


class TestParseQuery:
    def test_no_quotes_returns_empty_phrases_and_full_free_text(self) -> None:
        phrases, free = _parse_query("安全生产管理")
        assert phrases == []
        assert free == "安全生产管理"

    def test_english_double_quotes_extract_phrase(self) -> None:
        phrases, free = _parse_query('搜索"安全生产"相关内容')
        assert phrases == ["安全生产"]
        assert "搜索" in free
        assert "相关内容" in free

    def test_chinese_double_quotes_extract_phrase(self) -> None:
        phrases, free = _parse_query("关于“安全生产”的通知")  # “ = ", ” = "
        assert phrases == ["安全生产"]
        assert "关于" in free
        assert "的通知" in free

    def test_multiple_quoted_phrases(self) -> None:
        phrases, free = _parse_query('"安全生产"“消防管理”检查')
        assert phrases == ["安全生产", "消防管理"]
        assert "检查" in free

    def test_only_quoted_text(self) -> None:
        phrases, free = _parse_query('"安全生产"')
        assert phrases == ["安全生产"]
        assert free == ""

    def test_empty_string(self) -> None:
        phrases, free = _parse_query("")
        assert phrases == []
        assert free == ""


class TestBuildEsQuery:
    def test_basic_keyword_query(self) -> None:
        body = _build_es_query("安全生产")
        assert "query" in body
        must = body["query"]["bool"]["must"]
        assert len(must) == 1
        assert "multi_match" in must[0]
        assert must[0]["multi_match"]["query"] == "安全生产"
        assert must[0]["multi_match"]["fields"] == ["chunk_text", "title"]
        assert must[0]["multi_match"]["type"] == "best_fields"
        # Short query (len <= 4) enables fuzzy
        assert must[0]["multi_match"]["fuzziness"] == "AUTO"

    def test_long_query_no_fuzzy(self) -> None:
        body = _build_es_query("安全生产管理制度")
        must = body["query"]["bool"]["must"]
        assert "fuzziness" not in must[0]["multi_match"]

    def test_phrase_from_quotes(self) -> None:
        body = _build_es_query('关于"安全生产"管理')
        must = body["query"]["bool"]["must"]
        assert len(must) == 2
        # First clause: match_phrase for quoted part
        assert "match_phrase" in must[0]
        assert must[0]["match_phrase"]["chunk_text"]["query"] == "安全生产"
        # Second clause: multi_match for free text
        assert "multi_match" in must[1]
        assert "管理" in must[1]["multi_match"]["query"]

    def test_document_id_filter(self) -> None:
        body = _build_es_query("安全", document_id=42)
        filters = body["query"]["bool"]["filter"]
        assert {"term": {"document_id": 42}} in filters

    def test_doc_type_filter(self) -> None:
        body = _build_es_query("安全", doc_type="通知")
        filters = body["query"]["bool"]["filter"]
        assert {"term": {"doc_type": "通知"}} in filters

    def test_tags_filter(self) -> None:
        body = _build_es_query("安全", tags=["生产", "消防"])
        filters = body["query"]["bool"]["filter"]
        assert {"terms": {"tags": ["生产", "消防"]}} in filters

    def test_combined_filters(self) -> None:
        body = _build_es_query("安全", document_id=42, doc_type="通知", tags=["生产"])
        filters = body["query"]["bool"]["filter"]
        assert len(filters) == 3

    def test_custom_search_fields(self) -> None:
        body = _build_es_query("安全", search_fields=["chunk_text", "annotations.message"])
        mm = body["query"]["bool"]["must"][0]["multi_match"]
        assert mm["fields"] == ["chunk_text", "annotations.message"]

    def test_highlight_generated(self) -> None:
        body = _build_es_query("安全")
        assert "highlight" in body
        assert "chunk_text" in body["highlight"]["fields"]
        assert "title" in body["highlight"]["fields"]

    def test_query_truncated_at_500_chars(self) -> None:
        long_query = "x" * 600
        body = _build_es_query(long_query)
        mm = body["query"]["bool"]["must"][0]["multi_match"]
        assert len(mm["query"]) <= 500


class TestCleanResponse:
    RAW_ES_RESPONSE = {
        "took": 12,
        "timed_out": False,
        "_shards": {"total": 5, "successful": 5, "skipped": 0, "failed": 0},
        "hits": {
            "total": {"value": 2, "relation": "eq"},
            "max_score": 1.5,
            "hits": [
                {
                    "_index": "chunks",
                    "_id": "42_3",
                    "_score": 1.5,
                    "_source": {
                        "document_id": 42,
                        "doc_type": "通知",
                        "title": "关于加强安全生产的通知",
                        "chunk_text": "为进一步加强安全生产管理...",
                        "paragraph_index": 3,
                        "tags": ["安全", "生产"],
                        "author": "张三",
                        "publish_date": "2024-03-15",
                        "resource_id": 100,
                        "source_id": 200,
                        "chunk_no": 1,
                        "char_count": 500,
                        "audit_status": "reviewed",
                        "created_at": "2024-03-15T10:00:00Z",
                        "user_id": "user001",
                        "annotations": [{"type": "correction", "message": "..."}],
                    },
                    "highlight": {
                        "chunk_text": ["为进一步加强<em>安全生产</em>管理..."],
                    },
                },
                {
                    "_index": "chunks",
                    "_id": "99_1",
                    "_score": 0.8,
                    "_source": {
                        "document_id": 99,
                        "doc_type": "函",
                        "title": "关于消防安全检查的函",
                        "chunk_text": "根据消防管理规定...",
                        "paragraph_index": 1,
                        "tags": ["消防"],
                        "author": "李四",
                        "publish_date": "2024-02-20",
                    },
                    # No highlight — fallback to chunk_text preview
                },
            ],
        },
    }

    def test_total_and_took_ms(self) -> None:
        result = _clean_response(self.RAW_ES_RESPONSE)
        assert result["total"] == 2
        assert result["took_ms"] == 12

    def test_es_internals_stripped(self) -> None:
        result = _clean_response(self.RAW_ES_RESPONSE)
        for hit in result["hits"]:
            assert "_index" not in hit
            assert "_id" not in hit
            assert "_score" not in hit

    def test_whitelisted_fields_present(self) -> None:
        result = _clean_response(self.RAW_ES_RESPONSE)
        hit = result["hits"][0]
        assert hit["document_id"] == 42
        assert hit["doc_type"] == "通知"
        assert hit["title"] == "关于加强安全生产的通知"
        assert hit["paragraph_index"] == 3
        assert hit["tags"] == ["安全", "生产"]
        assert hit["author"] == "张三"
        assert hit["publish_date"] == "2024-03-15"

    def test_noise_fields_excluded(self) -> None:
        result = _clean_response(self.RAW_ES_RESPONSE)
        hit = result["hits"][0]
        assert "char_count" not in hit
        assert "audit_status" not in hit
        assert "created_at" not in hit
        assert "user_id" not in hit

    def test_annotations_excluded_by_default(self) -> None:
        result = _clean_response(self.RAW_ES_RESPONSE, include_annotations=False)
        assert "annotations" not in result["hits"][0]

    def test_annotations_included_when_requested(self) -> None:
        result = _clean_response(self.RAW_ES_RESPONSE, include_annotations=True)
        assert "annotations" in result["hits"][0]
        assert result["hits"][0]["annotations"][0]["type"] == "correction"

    def test_highlight_extracted(self) -> None:
        result = _clean_response(self.RAW_ES_RESPONSE)
        hit = result["hits"][0]
        assert "highlight" in hit
        assert "<em>安全生产</em>" in hit["highlight"][0]

    def test_fallback_when_no_highlight(self) -> None:
        result = _clean_response(self.RAW_ES_RESPONSE)
        hit = result["hits"][1]
        assert "highlight" not in hit
        assert "chunk_text_preview" in hit
        assert len(hit["chunk_text_preview"]) <= 300


class TestSearchDocumentsTool:
    def test_name(self) -> None:
        tool = SearchDocumentsTool()
        assert tool.name == "search_documents"

    def test_parameters_require_query(self) -> None:
        tool = SearchDocumentsTool()
        assert "query" in tool.parameters["required"]
        assert "query_body" not in tool.parameters["required"]
        assert "query_body" not in tool.parameters["properties"]

    @pytest.mark.asyncio
    async def test_execute_empty_query_returns_error(self) -> None:
        tool = SearchDocumentsTool()
        result = await tool.execute(query="   ")
        assert result.success is False
        assert "不能为空" in result.error

    @pytest.mark.asyncio
    async def test_execute_searches_with_new_params(self) -> None:
        from unittest.mock import patch

        with patch("src.es.client.search_chunks") as mock_search:
            mock_search.return_value = {
                "took": 5,
                "hits": {
                    "total": {"value": 1},
                    "hits": [
                        {
                            "_source": {
                                "document_id": 1,
                                "doc_type": "通知",
                                "title": "测试",
                                "chunk_text": "测试内容",
                                "paragraph_index": 0,
                            },
                            "highlight": {"chunk_text": ["测试<em>内容</em>"]},
                        }
                    ],
                },
            }
            tool = SearchDocumentsTool()
            result = await tool.execute(
                query="测试内容",
                document_id=1,
                skip=0,
                limit=10,
            )
            assert result.success
            assert result.data["total"] == 1
            assert result.data["took_ms"] == 5
            assert result.data["hits"][0]["title"] == "测试"
            assert "highlight" in result.data["hits"][0]
            # Verify built query is not raw DSL
            call_args = mock_search.call_args
            es_body = call_args.kwargs["query_body"]
            assert "multi_match" in str(es_body) or "match_phrase" in str(es_body)
            assert call_args.kwargs["skip"] == 0
            assert call_args.kwargs["limit"] == 10

    @pytest.mark.asyncio
    async def test_execute_default_skip_limit(self) -> None:
        from unittest.mock import patch

        with patch("src.es.client.search_chunks") as mock_search:
            mock_search.return_value = {
                "took": 0,
                "hits": {"total": {"value": 0}, "hits": []},
            }
            tool = SearchDocumentsTool()
            result = await tool.execute(query="无匹配结果查询")
            assert result.success
            assert result.data["total"] == 0
            mock_search.assert_called_once()
            assert mock_search.call_args.kwargs["skip"] == 0
            assert mock_search.call_args.kwargs["limit"] == 10

    @pytest.mark.asyncio
    async def test_execute_es_error(self) -> None:
        from unittest.mock import patch

        with patch("src.es.client.search_chunks") as mock_search:
            mock_search.side_effect = RuntimeError("ES connection failed")
            tool = SearchDocumentsTool()
            result = await tool.execute(query="安全")
            assert result.success is False
            assert "ES connection failed" in result.error
```

- [ ] **Step 2: Run tests, verify they all fail**

```bash
python -m pytest tests/agent/tools/test_search.py -v 2>&1 | tail -30
```

Expected: FAIL — `ImportError` because `_parse_query`, `_build_es_query`, `_clean_response` don't exist yet.

- [ ] **Step 3: Commit tests**

```bash
git add tests/agent/tools/test_search.py
git commit -m "test: add query builder, response cleaner, and tool execution tests for search_documents"
```

---

### Task 2: Implement query builder and response cleaner

**Files:**
- Modify: `src/agent/skills/search/tool.py`

- [ ] **Step 1: Rewrite tool.py with new functions and tool class**

```python
"""Search tool — LLM-friendly Elasticsearch document search."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from src.agent.tools.protocol import ToolResult

logger = logging.getLogger(__name__)

_QUOTE_RE = re.compile(
    r'["“”‘’「」](.+?)["“”‘’「」]'
)

_FUZZY_MAX_LEN = 4
_MAX_QUERY_CHARS = 500
_FALLBACK_PREVIEW_CHARS = 300

_INCLUDE_FIELDS = frozenset({
    "document_id", "doc_type", "title", "chunk_text",
    "paragraph_index", "tags", "author", "publish_date",
    "resource_id", "source_id", "chunk_no",
})


def _parse_query(query: str) -> tuple[list[str], str]:
    """Extract quoted phrases and remaining free text from query string.

    Returns (phrases, free_text). Phrases go to match_phrase,
    free_text goes to multi_match.
    """
    phrases: list[str] = []
    free_parts: list[str] = []
    last_end = 0

    for match in _QUOTE_RE.finditer(query):
        if match.start() > last_end:
            free_parts.append(query[last_end:match.start()])
        phrases.append(match.group(1))
        last_end = match.end()

    if last_end < len(query):
        free_parts.append(query[last_end:])

    free_text = " ".join(p.strip() for p in free_parts if p.strip())
    return phrases, free_text


def _build_es_query(
    query: str,
    document_id: int | None = None,
    doc_type: str | None = None,
    tags: list[str] | None = None,
    search_fields: list[str] | None = None,
) -> dict[str, Any]:
    """Build ES query body from simplified parameters."""
    if len(query) > _MAX_QUERY_CHARS:
        query = query[:_MAX_QUERY_CHARS]
        logger.warning("Query truncated to %d chars", _MAX_QUERY_CHARS)

    fields = search_fields or ["chunk_text", "title"]
    phrases, free_text = _parse_query(query)

    must: list[dict] = []

    for phrase in phrases:
        must.append({
            "match_phrase": {fields[0]: {"query": phrase, "slop": 0}}
        })

    if free_text.strip():
        mm: dict[str, Any] = {
            "query": free_text.strip(),
            "fields": fields,
            "type": "best_fields",
        }
        if len(free_text.strip()) <= _FUZZY_MAX_LEN:
            mm["fuzziness"] = "AUTO"
        must.append({"multi_match": mm})

    query_dict: dict[str, Any] = {"bool": {}}
    if must:
        query_dict["bool"]["must"] = must

    filter_clauses: list[dict] = []
    if document_id is not None:
        filter_clauses.append({"term": {"document_id": document_id}})
    if doc_type is not None:
        filter_clauses.append({"term": {"doc_type": doc_type}})
    if tags:
        filter_clauses.append({"terms": {"tags": tags}})
    if filter_clauses:
        query_dict["bool"]["filter"] = filter_clauses

    body: dict[str, Any] = {"query": query_dict}

    hl_fields: dict[str, dict] = {}
    for f in fields:
        hl_fields[f] = {}
    if hl_fields:
        body["highlight"] = {"fields": hl_fields}

    return body


def _clean_response(raw: dict, include_annotations: bool = False) -> dict[str, Any]:
    """Clean raw ES response into LLM-friendly format."""
    hits_raw = raw.get("hits", {})
    hits_list = hits_raw.get("hits", [])
    total = hits_raw.get("total", {})
    total_value = total.get("value", 0) if isinstance(total, dict) else total

    cleaned_hits: list[dict] = []
    for hit in hits_list:
        source = hit.get("_source", {})
        entry: dict[str, Any] = {}

        for field in _INCLUDE_FIELDS:
            if field in source:
                entry[field] = source[field]

        if include_annotations and "annotations" in source:
            entry["annotations"] = source["annotations"]

        highlight = hit.get("highlight", {})
        snippets: list[str] = []
        for field_snippets in highlight.values():
            if isinstance(field_snippets, list):
                snippets.extend(field_snippets)
        if snippets:
            entry["highlight"] = snippets
        else:
            ct = source.get("chunk_text", "")
            if ct:
                entry["chunk_text_preview"] = ct[:_FALLBACK_PREVIEW_CHARS]

        cleaned_hits.append(entry)

    return {
        "total": total_value,
        "took_ms": raw.get("took", 0),
        "hits": cleaned_hits,
    }


class SearchDocumentsTool:
    """LLM-friendly document chunk search via Elasticsearch.

    Translates semantic query parameters into ES Query DSL internally
    and returns cleaned, structured results stripped of ES internals.
    """

    name: str = "search_documents"
    description: str = (
        "在已索引的文档块中搜索内容。只需传入搜索关键词，系统会自动构建查询并格式化结果。"
        "用引号包裹的词会作为精确短语匹配（如 \"安全生产\" 匹配完整短语），"
        "其余部分作为关键词分词匹配。支持按文档ID、文档类型、标签过滤。"
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "搜索关键词或短语。中英文引号包裹的文本做精确短语匹配，"
                    "其余文本做关键词分词匹配。示例：关于\"安全生产\"的通知"
                ),
            },
            "document_id": {
                "type": "integer",
                "description": "限定搜索指定文档 ID 的范围",
            },
            "doc_type": {
                "type": "string",
                "description": "按文档类型过滤（如 通知、函、请示）",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "按标签过滤，返回包含任一指定标签的文档块",
            },
            "search_fields": {
                "type": "array",
                "items": {"type": "string"},
                "description": "指定搜索字段，默认 ['chunk_text', 'title']",
            },
            "include_annotations": {
                "type": "boolean",
                "description": "是否在结果中包含标注详情，默认不包含以减少数据量",
            },
            "skip": {
                "type": "integer",
                "description": "跳过的结果数，用于分页，默认 0",
            },
            "limit": {
                "type": "integer",
                "description": "返回的最大结果数，默认 10",
            },
        },
        "required": ["query"],
    }

    async def execute(self, **kwargs: Any) -> ToolResult:
        query: str = kwargs.get("query", "").strip()
        if not query:
            return ToolResult(success=False, error="查询内容不能为空")

        try:
            es_body = _build_es_query(
                query=query,
                document_id=kwargs.get("document_id"),
                doc_type=kwargs.get("doc_type"),
                tags=kwargs.get("tags"),
                search_fields=kwargs.get("search_fields"),
            )
        except Exception as exc:
            return ToolResult(success=False, error=f"查询构建失败: {exc}")

        try:
            from src.es.client import search_chunks

            raw = await asyncio.to_thread(
                search_chunks,
                query_body=es_body,
                skip=kwargs.get("skip", 0),
                limit=kwargs.get("limit", 10),
            )
        except Exception as exc:
            return ToolResult(success=False, error=str(exc))

        try:
            cleaned = _clean_response(
                raw,
                include_annotations=bool(kwargs.get("include_annotations", False)),
            )
        except Exception as exc:
            return ToolResult(success=False, error=f"结果处理失败: {exc}")

        return ToolResult(success=True, data=cleaned)


def create_search_documents_tool() -> SearchDocumentsTool:
    """Factory function for backward compatibility with existing imports."""
    return SearchDocumentsTool()
```

- [ ] **Step 2: Run unit tests, verify all pass**

```bash
python -m pytest tests/agent/tools/test_search.py -v 2>&1
```

Expected: all tests PASS.

- [ ] **Step 3: Commit implementation**

```bash
git add src/agent/skills/search/tool.py
git commit -m "feat: replace ES DSL with LLM-friendly search interface

Add _parse_query for quote detection, _build_es_query for ES query
construction from semantic params, and _clean_response for stripping
ES internals from results. Remove query_body parameter."
```

---

### Task 3: Update SKILL.md documentation

**Files:**
- Modify: `src/agent/skills/search/SKILL.md`

- [ ] **Step 1: Update SKILL.md**

```markdown
---
name: search
description: >-
  通过 Elasticsearch 搜索已索引的文档块。支持关键词搜索、精确短语匹配、
  按文档类型/标签过滤。触发关键词：搜索、检索、查询、search、find documents。
type: tool-wrapper
tools:
  - search_documents
priority: 5
---

# 文档搜索

## 能力
通过自然语言关键词搜索已索引的文档块，内部自动构建 Elasticsearch 查询。
支持精确短语匹配（引号包裹）、关键词分词匹配、以及按文档 ID/类型/标签过滤。

## 使用方式
调用 `search_documents` 工具，传入 `query`（搜索关键词）。

## 参数
- `query`: 搜索关键词或短语（必填）。引号包裹部分做精确短语匹配，其余做关键词匹配
- `document_id`: 限定文档 ID（可选）
- `doc_type`: 文档类型过滤，如 通知、函、请示（可选）
- `tags`: 标签过滤（可选）
- `search_fields`: 指定搜索字段，默认 `["chunk_text", "title"]`（可选）
- `include_annotations`: 是否包含标注详情，默认 false（可选）
- `skip`: 跳过的结果数，默认 0（可选）
- `limit`: 返回的最大结果数，默认 10（可选）

## 返回格式
```json
{
  "total": 156,
  "took_ms": 12,
  "hits": [
    {
      "document_id": 42,
      "doc_type": "通知",
      "title": "关于加强安全生产管理的通知",
      "chunk_text": "匹配的文本内容...",
      "paragraph_index": 3,
      "highlight": ["...<em>关键词</em>..."],
      "tags": ["安全"],
      "author": "xxx",
      "publish_date": "2024-03-15"
    }
  ]
}
```

## 注意事项
- 需要 ES 服务可用
- 短关键词（≤4 字符）自动启用模糊匹配（fuzzy）
- 长查询超过 500 字符会被截断
- `annotations` 字段默认不返回，需显式设置 `include_annotations: true`
- 无 highlight 时返回 `chunk_text` 前 300 字符作为 `chunk_text_preview`
```

- [ ] **Step 2: Commit documentation**

```bash
git add src/agent/skills/search/SKILL.md
git commit -m "docs: update search skill docs for simplified interface"
```

---

### Task 4: Run full test suite and fix regressions

- [ ] **Step 1: Run all tests**

```bash
python -m pytest tests/ -x -q --tb=short --ignore=tests/scripts 2>&1
```

Expected: All tests pass. The only potentially affected external test is `test_all_tools_registry.py` which checks tool count (21 tools, unchanged since we still register exactly one `SearchDocumentsTool`).

- [ ] **Step 2: Fix any broken tests**

If `test_all_tools_registry.py::test_tool_count` or similar fails due to schema changes, update the assertions. No count changes are expected since we only modified the tool's internal behavior, not its registration.

---

### Task 5: Final verification and commit

- [ ] **Step 1: Verify the complete tool chain imports correctly**

```bash
python -c "
from src.agent.skills.search.tool import SearchDocumentsTool, _parse_query, _build_es_query, _clean_response, create_search_documents_tool
t = create_search_documents_tool()
print('Tool name:', t.name)
print('Required params:', t.parameters['required'])
p, f = _parse_query('关于\"安全生产\"的通知')
print('Phrases:', p, 'Free:', f)
body = _build_es_query('安全')
print('ES body keys:', list(body.keys()))
"
```

Expected: No errors, prints tool info and parsed query.

- [ ] **Step 2: Run full test suite one final time**

```bash
python -m pytest tests/ -x -q --tb=short --ignore=tests/scripts 2>&1 | tail -5
```

Expected: All tests pass.

- [ ] **Step 3: Verify nothing was missed**

```bash
git status
git diff --stat HEAD
```
