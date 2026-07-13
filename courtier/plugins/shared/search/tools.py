"""Search tool — LLM-friendly Elasticsearch document search."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from courtier.agent.tools.protocol import ToolResult

logger = logging.getLogger(__name__)

_QUOTE_RE = re.compile(
    r"[\u0022\u201c\u201d\u2018\u2019\u300c\u300d]"
    r"(.+?)"
    r"[\u0022\u201c\u201d\u2018\u2019\u300c\u300d]"
)

_FUZZY_MAX_LEN = 4
_MAX_QUERY_CHARS = 500
_MAX_SKIP = 10_000
_MAX_LIMIT = 100
_LOW_SIGNAL_QUERY_RE = re.compile(r"^[\W_]+$", re.UNICODE)
_FALLBACK_PREVIEW_CHARS = 300

_INCLUDE_FIELDS = frozenset(
    {
        "document_id",
        "doc_type",
        "title",
        "chunk_text",
        "paragraph_index",
        "tags",
        "author",
        "publish_date",
        "resource_id",
        "source_id",
        "chunk_no",
    }
)


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
            free_parts.append(query[last_end : match.start()])
        phrases.append(match.group(1))
        last_end = match.end()

    if last_end < len(query):
        free_parts.append(query[last_end:])

    free_text = " ".join(p.strip() for p in free_parts if p.strip())
    return phrases, free_text


def _has_search_signal(query: str) -> bool:
    """Return True when query contains meaningful alphanumeric or CJK content."""
    stripped = query.strip()
    if len(stripped) < 2:
        return False
    return _LOW_SIGNAL_QUERY_RE.match(stripped) is None


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
        must.append({"match_phrase": {fields[0]: {"query": phrase, "slop": 0}}})

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
        '用引号包裹的词会作为精确短语匹配（如 "安全生产" 匹配完整短语），'
        "其余部分作为关键词分词匹配。支持按文档ID、文档类型、标签过滤。"
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "搜索关键词或短语。中英文引号包裹的文本做精确短语匹配，"
                    '其余文本做关键词分词匹配。示例：关于"安全生产"的通知'
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
    output_artifact_type: str | None = "docaudit.search_results"

    async def execute(self, **kwargs: Any) -> ToolResult:
        query: str = kwargs.get("query", "").strip()
        if not query:
            return ToolResult(success=False, error="查询内容不能为空")
        if not _has_search_signal(query):
            return ToolResult(success=False, error="查询内容过短或缺少有效字符")

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
            from courtier.es.client import search_chunks

            skip = kwargs.get("skip", 0)
            limit = kwargs.get("limit", 10)
            if not isinstance(skip, int) or skip < 0 or skip > _MAX_SKIP:
                return ToolResult(
                    success=False,
                    error=f"skip 必须是 0 到 {_MAX_SKIP} 之间的整数",
                )
            if not isinstance(limit, int) or limit < 1 or limit > _MAX_LIMIT:
                return ToolResult(
                    success=False,
                    error=f"limit 必须是 1 到 {_MAX_LIMIT} 之间的整数",
                )
            raw = await asyncio.to_thread(
                search_chunks,
                query_body=es_body,
                skip=skip,
                limit=limit,
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
