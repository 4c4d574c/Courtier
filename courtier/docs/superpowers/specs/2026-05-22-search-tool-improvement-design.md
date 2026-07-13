# Search Tool Improvement — LLM-Friendly Search

**Date:** 2026-05-22
**Status:** approved

## Motivation

The current `search_documents` tool requires the LLM to write raw Elasticsearch Query DSL (e.g., `{"query": {"match": {"chunk_text": "keyword"}}}`), which is error-prone and unnecessarily complex for an LLM. Additionally, the raw ES response includes noisy internal fields (`_index`, `_score`, `_shards`, etc.) that waste context window budget.

## Design

### 1. Query Interface (Simplified)

Replace `query_body` with semantic parameters:

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `query` | string | yes | — | Search keywords or phrase. Quoted text (Chinese/English quotes) triggers exact phrase match via `match_phrase`. |
| `document_id` | integer | no | — | Filter to a specific document. |
| `doc_type` | string | no | — | Filter by document type (e.g., `通知`, `函`). |
| `tags` | string[] | no | — | Filter by tags. |
| `search_fields` | string[] | no | `["chunk_text", "title"]` | Fields to search against. |
| `include_annotations` | boolean | no | `false` | Whether to include annotation details in results. |
| `skip` | integer | no | `0` | Pagination offset. |
| `limit` | integer | no | `10` | Max results. |

**Backward compatibility:** `query_body` is removed entirely.

### 2. Query Building Logic

The tool internally constructs an ES bool query:

1. **Quote detection** — Text inside Chinese (`""` / `''`) or English (`""` / `''`) quotes becomes a `match_phrase` with configurable slop (default 0, exact phrase). Unquoted text becomes a `multi_match` with `best_fields` strategy.

2. **Fuzzy tolerance** — Automatically enabled for query terms ≤ 4 characters via `fuzziness: "AUTO"`. Longer terms skip fuzzy to avoid false positives.

3. **Filters** — `document_id`, `doc_type`, and `tags` are applied as `filter` clauses (not scoring) for performance.

4. **Highlight** — Always enabled on `search_fields` to help LLM locate matching content.

Example generated query structure:

```json
{
  "query": {
    "bool": {
      "must": [
        {"match_phrase": {"chunk_text": {"query": "安全生产", "slop": 0}}},
        {"multi_match": {"query": "管理", "fields": ["chunk_text", "title"], "type": "best_fields", "fuzziness": "AUTO"}}
      ],
      "filter": [
        {"term": {"doc_type": "通知"}}
      ]
    }
  },
  "highlight": {
    "fields": {"chunk_text": {}, "title": {}}
  }
}
```

### 3. Response Format (Cleaned)

Noise-free structured results, ES internals stripped:

```json
{
  "total": 156,
  "took_ms": 12,
  "hits": [
    {
      "document_id": 42,
      "doc_type": "通知",
      "title": "关于加强安全生产管理的通知",
      "chunk_text": "...matching text content...",
      "paragraph_index": 3,
      "highlight": ["...<em>安全生产</em>...", "...<em>管理</em>..."],
      "tags": ["安全", "生产"],
      "author": "xxx",
      "publish_date": "2024-03-15"
    }
  ]
}
```

- `annotations` is omitted by default to save context; opt-in via `include_annotations: true`.
- If highlight returns empty, `chunk_text` is truncated to first 300 chars as fallback.

### 4. Error Handling

| Scenario | Behavior |
|---|---|
| `query` is empty | Return error: "查询内容不能为空" |
| ES service unavailable | Return error with original exception message |
| Zero results (total=0) | Return success with empty hits array |
| Stop words only | Execute normally; ES handles low-relevance results |
| Non-existent `document_id` | Filter yields empty results (normal ES behavior) |
| Query > 500 chars | Truncate to 500 chars, log warning |
| File missing or corrupted | Return clear error message |

### 5. File Changes

- **Modify:** `src/agent/skills/search/tool.py` — Rewrite tool class with new interface, query builder, and response cleaner
- **Modify:** `src/agent/skills/search/SKILL.md` — Update documentation
- **Add:** `tests/agent/tools/test_search.py` — Unit tests for query building and response formatting

### 6. Testing

- **Unit tests:** Quote parsing, fuzzy enable/disable logic, filter assembly, response cleaning
- **Integration tests:** End-to-end search against real ES (skip if ES unavailable)
- **Regression:** Verify tool count unchanged (1 search tool)

## Non-Goals

- No semantic/vector search — separate future improvement
- No aggregation/faceting support
- No sorting customization (always by relevance score)
- No result merging across multiple queries
