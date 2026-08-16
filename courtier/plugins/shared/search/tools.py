"""Search tool — LLM-friendly Elasticsearch document search."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import logging
import re
import threading
import time
from collections import OrderedDict
from datetime import date
from typing import Any

from courtier_plugin_sdk import ToolResult

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

#: Default search fields with title weighted above body text.
_DEFAULT_FIELDS_WEIGHTED = ["chunk_text^1", "title^3"]
#: Free text also runs as a (boosted) phrase query so documents containing
#: the exact phrase rank above scattered keyword matches.
_FREE_PHRASE_BOOST = 2.0
_FREE_PHRASE_SLOP = 2
#: Queries with >= this many space-separated words use minimum_should_match
#: instead of operator AND to avoid over-constraining long queries.
_MULTI_WORD_THRESHOLD = 4
#: Same fallback for long unspaced Chinese queries: an 8-char string yields
#: ~7 bigrams, and requiring ALL of them kills recall on any paraphrase
#: (measured: 4 golden queries at 6-8 chars returned zero hits at AND).
_LONG_QUERY_CHARS = 6
_MIN_SHOULD_MATCH_MULTI_WORD = "70%"

#: Query-side synonym expansion for official-document terminology.  ES
#: token-level synonym filters cannot match multi-char Chinese synonyms under
#: bigram tokenization, so equivalents are expanded client-side into optional
#: (should) phrase boosters — they widen recall without over-constraining.
SYNONYM_MAP: dict[str, tuple[str, ...]] = {
    "安监局": ("安监局", "安全生产监督管理局"),
    "安全生产监督管理局": ("安全生产监督管理局", "安监局"),
    "通知": ("通知", "印发", "转发"),
    "印发": ("印发", "通知", "转发"),
    "转发": ("转发", "通知", "印发"),
    "办法": ("办法", "规定"),
    "规定": ("规定", "办法"),
    "批复": ("批复", "复函"),
    "复函": ("复函", "批复"),
}
_SYNONYM_BOOST = 1.5

#: Neighbor context expansion: chunks within +/- this window of a hit are
#: attached as `neighbors` so provisions spanning chunk boundaries are
#: returned as a unit.
_NEIGHBOR_WINDOW = 2
_NEIGHBOR_PREVIEW_CHARS = 300

#: Hybrid retrieval knobs.  ES `rank.rrf` needs a commercial license, so
#: fusion is done client-side: run the lexical query and a kNN query, then
#: merge with reciprocal rank fusion in-process.
_KNN_K = 50
_KNN_NUM_CANDIDATES = 200
_RRF_RANK_CONSTANT = 60

#: How many rough-ranked hits rerank mode fetches before LLM listwise
#: reordering (then slices to the requested limit).
_RERANK_MAX_FETCH = 100

#: Hybrid/rerank pages slice an in-process fused candidate list, so both
#: arms fetch a common window of this size; paging deeper than the window
#: yields empty pages (use filters to narrow instead).
_MAX_WINDOW = 200

#: Optional publish_date recency weighting (gauss decay on the lexical arm).
_TIME_DECAY_SCALE = "730d"
_TIME_DECAY_DECAY = 0.5
#: Client-side twin of the ES gauss decay, applied on the fused scores when
#: hybrid retrieval is planned (ES function_score cannot decorate the kNN
#: arm, so both arms decay consistently only when done here).
_TIME_DECAY_SCALE_DAYS = 730.0


def _decay_multiplier(publish_date: str | None, now: date) -> float:
    """ES-gauss-shaped score multiplier: decay ** ((age/scale) ** 2).

    Chunks without a publish date stay neutral (multiplier 1.0), mirroring
    the neutral weight function the server-side decay uses for undated docs.
    """
    if not publish_date:
        return 1.0
    try:
        published = date.fromisoformat(str(publish_date)[:10])
    except ValueError:
        return 1.0
    age_days = max((now - published).days, 0)
    return _TIME_DECAY_DECAY ** ((age_days / _TIME_DECAY_SCALE_DAYS) ** 2)

#: Process-local TTL cache for coarse search results (pre-rerank).  Rerank
#: results are recomputed per call (LLM nondeterminism); neighbor expansion
#: is cached only on the non-rerank path where the final hit set is stable.
_CACHE_TTL_SECONDS = 120.0
_CACHE_MAX_ENTRIES = 256
_cache: OrderedDict[str, tuple[float, dict]] = OrderedDict()
_cache_lock = threading.Lock()


def _cache_key(rerank: bool, **kwargs: Any) -> str:
    """Deterministic cache key over the query parameters plus owner scope."""
    parts = [
        str(kwargs.get("_owner_scope")),
        str(kwargs.get("query", "")),
        str(kwargs.get("document_id")),
        str(kwargs.get("doc_type") or ""),
        ",".join(str(t) for t in (kwargs.get("tags") or [])),
        ",".join(str(f) for f in (kwargs.get("search_fields") or [])),
        str(kwargs.get("skip", 0)),
        str(kwargs.get("limit", 10)),
        str(bool(kwargs.get("include_annotations", False))),
        str(bool(kwargs.get("use_time_decay", False))),
        str(bool(rerank)),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _cache_get(key: str) -> dict | None:
    with _cache_lock:
        entry = _cache.get(key)
        if entry is None:
            return None
        expires, data = entry
        if time.monotonic() > expires:
            _cache.pop(key, None)
            return None
        _cache.move_to_end(key)
        return data


def _cache_put(key: str, data: dict) -> None:
    with _cache_lock:
        _cache[key] = (time.monotonic() + _CACHE_TTL_SECONDS, data)
        _cache.move_to_end(key)
        while len(_cache) > _CACHE_MAX_ENTRIES:
            _cache.popitem(last=False)


def _cache_clear() -> None:
    with _cache_lock:
        _cache.clear()


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

# Sentinel distinguishing "host injected no scope" (internal/test callers —
# no visibility filter) from "injected None" (anonymous — public only).
_QUERY_UNSET = object()


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


def _owner_scope_filter(owner_scope: Any) -> list[dict]:
    """Visibility filter clauses for *owner_scope* (host-injected, never
    model-supplied).  Empty for internal/test callers (_QUERY_UNSET)."""
    if owner_scope is _QUERY_UNSET:
        return []
    should_scope: list[dict] = [{"term": {"visibility": "public"}}]
    if isinstance(owner_scope, int):
        should_scope.append({"term": {"owner_id": owner_scope}})
    # Chunks indexed before the visibility split are legacy public data.
    should_scope.append({"bool": {"must_not": [{"exists": {"field": "visibility"}}]}})
    return [{"bool": {"should": should_scope, "minimum_should_match": 1}}]


def _build_filters(
    document_id: int | None,
    doc_type: str | None,
    tags: list[str] | None,
    owner_scope: Any,
) -> list[dict]:
    """Exact filter clauses shared by the lexical and kNN arms."""
    clauses: list[dict] = []
    if document_id is not None:
        clauses.append({"term": {"document_id": document_id}})
    if doc_type is not None:
        clauses.append({"term": {"doc_type": doc_type}})
    if tags:
        clauses.append({"terms": {"tags": tags}})
    clauses.extend(_owner_scope_filter(owner_scope))
    return clauses


def _build_es_query(
    query: str,
    document_id: int | None = None,
    doc_type: str | None = None,
    tags: list[str] | None = None,
    search_fields: list[str] | None = None,
    owner_scope: Any = _QUERY_UNSET,
    use_time_decay: bool = False,
) -> dict[str, Any]:
    """Build the lexical ES query body from simplified parameters.

    *owner_scope* is host-injected (never model-supplied): an int restricts
    results to public chunks + the caller's own personal chunks; ``None``
    (anonymous) restricts to public only.  ``_QUERY_UNSET`` (internal/test
    callers) applies no visibility filter.  Chunks predating the visibility
    field are treated as public.
    """
    if len(query) > _MAX_QUERY_CHARS:
        query = query[:_MAX_QUERY_CHARS]
        logger.warning("Query truncated to %d chars", _MAX_QUERY_CHARS)

    fields = search_fields or _DEFAULT_FIELDS_WEIGHTED
    # match_phrase takes plain field names (weights are invalid there).
    phrase_fields = [f.split("^")[0] for f in fields]
    phrases, free_text = _parse_query(query)

    must: list[dict] = []
    should: list[dict] = []

    for phrase in phrases:
        must.append({"match_phrase": {phrase_fields[0]: {"query": phrase, "slop": 0}}})

    if free_text.strip():
        stripped = free_text.strip()
        mm: dict[str, Any] = {
            "query": stripped,
            "fields": fields,
            "type": "best_fields",
        }
        if len(stripped) <= _FUZZY_MAX_LEN:
            mm["fuzziness"] = "AUTO"
        # Require (nearly) all keywords instead of OR semantics: with
        # per-character/bigram tokenization, OR makes multi-word queries
        # match the whole corpus.
        if len(stripped.split()) >= _MULTI_WORD_THRESHOLD or len(stripped) > _LONG_QUERY_CHARS:
            mm["minimum_should_match"] = _MIN_SHOULD_MATCH_MULTI_WORD
        else:
            mm["operator"] = "and"
        must.append({"multi_match": mm})
        # Phrase signal: documents containing the full phrase rank above
        # scattered keyword hits.
        should.append(
            {
                "match_phrase": {
                    phrase_fields[0]: {
                        "query": stripped,
                        "slop": _FREE_PHRASE_SLOP,
                        "boost": _FREE_PHRASE_BOOST,
                    }
                }
            }
        )
        # Synonym equivalents as additional optional phrase boosters.
        seen: set[str] = set()
        for term, equivalents in SYNONYM_MAP.items():
            if term in stripped:
                for equivalent in equivalents:
                    if equivalent != stripped and equivalent not in seen:
                        seen.add(equivalent)
                        should.append(
                            {
                                "match_phrase": {
                                    phrase_fields[0]: {
                                        "query": equivalent,
                                        "slop": _FREE_PHRASE_SLOP,
                                        "boost": _SYNONYM_BOOST,
                                    }
                                }
                            }
                        )

    query_dict: dict[str, Any] = {"bool": {}}
    if must:
        query_dict["bool"]["must"] = must
    if should:
        query_dict["bool"]["should"] = should

    filter_clauses = _build_filters(document_id, doc_type, tags, owner_scope)
    if filter_clauses:
        query_dict["bool"]["filter"] = filter_clauses

    final_query: dict[str, Any] = query_dict
    if use_time_decay:
        # Recency weighting on the lexical arm: newer documents score
        # higher.  gauss decay lacks a `missing` parameter, so undated
        # chunks get an explicit neutral weight function instead.
        final_query = {
            "function_score": {
                "query": query_dict,
                "functions": [
                    {
                        "filter": {"exists": {"field": "publish_date"}},
                        "gauss": {
                            "publish_date": {
                                "origin": "now",
                                "scale": _TIME_DECAY_SCALE,
                                "decay": _TIME_DECAY_DECAY,
                            }
                        },
                    },
                    {
                        "filter": {"bool": {"must_not": [{"exists": {"field": "publish_date"}}]}},
                        "weight": 1.0,
                    },
                ],
                "boost_mode": "multiply",
            }
        }

    body: dict[str, Any] = {"query": final_query}

    # Deterministic ranking: relevance, then recency, then (resource_id,
    # chunk_no) which uniquely identify a chunk — tied scores never shuffle
    # between calls.  (_id sorting needs fielddata, disallowed by default.)
    body["sort"] = [
        {"_score": {"order": "desc"}},
        {"publish_date": {"order": "desc", "unmapped_type": "date"}},
        {"resource_id": {"order": "asc", "unmapped_type": "long"}},
        {"chunk_no": {"order": "asc", "unmapped_type": "long"}},
    ]

    hl_fields: dict[str, dict] = {}
    for f in phrase_fields:
        hl_fields[f] = {}
    if hl_fields:
        body["highlight"] = {"fields": hl_fields}

    return body


def _build_knn_query(
    query_vector: list[float],
    filter_clauses: list[dict],
    k: int = _KNN_K,
) -> dict:
    """kNN arm of hybrid retrieval.  Must carry the same filters as the
    lexical arm, otherwise fusion could surface documents the lexical
    filters excluded (visibility leak)."""
    knn: dict[str, Any] = {
        "field": "chunk_vector",
        "query_vector": query_vector,
        "k": k,
        "num_candidates": max(_KNN_NUM_CANDIDATES, k),
    }
    if filter_clauses:
        knn["filter"] = filter_clauses
    return {"knn": knn}


def _fusion_sort_key(hit: dict) -> tuple:
    """Ordering key for fused hits under reverse=True: RRF score desc,
    publish_date desc, then resource_id/chunk_no asc (tie-breaks)."""
    source = hit.get("_source", {}) or {}
    publish_date = source.get("publish_date") or ""
    return (
        hit.get("_rrf", 0.0),
        publish_date,
        -(source.get("resource_id", 0) or 0),
        -(source.get("chunk_no", 0) or 0),
    )


def _rrf_fuse(
    lexical_hits: list[dict],
    knn_hits: list[dict],
    decay_fn: Any = None,
) -> list[dict]:
    """Client-side reciprocal rank fusion of two hit lists.

    ES `rank.rrf` requires a commercial license, so fusion happens here:
    each hit scores sum(1 / (rank_constant + rank)) over the lists it
    appears in.  The lexical hit dict wins on collision (it carries the
    highlight snippets); the fused score lands in ``_rrf``.  *decay_fn*
    (hit → multiplier) is applied to ``_rrf`` before sorting — used for
    client-side publish_date recency weighting so both arms decay
    consistently.
    """
    merged: dict[str, dict] = {}
    scores: dict[str, float] = {}
    for hits in (lexical_hits, knn_hits):
        for rank, hit in enumerate(hits, start=1):
            hit_id = hit.get("_id")
            if hit_id is None:
                continue
            merged.setdefault(hit_id, hit)
            scores[hit_id] = scores.get(hit_id, 0.0) + 1.0 / (_RRF_RANK_CONSTANT + rank)
    for hit_id, hit in merged.items():
        score = scores[hit_id]
        if decay_fn is not None:
            score *= decay_fn(hit)
        hit["_rrf"] = score
    return sorted(merged.values(), key=_fusion_sort_key, reverse=True)


def _build_neighbor_query(hits: list[dict], owner_scope: Any) -> dict:
    """Second-stage query fetching the +/-_NEIGHBOR_WINDOW chunks around each
    hit, with the same visibility scope as the main query.  Returns an empty
    dict when no hit carries usable (resource_id, chunk_no) coordinates."""
    should_clauses: list[dict] = []
    for hit in hits:
        resource_id = hit.get("resource_id")
        chunk_no = hit.get("chunk_no")
        if resource_id is None or chunk_no is None:
            continue
        should_clauses.append(
            {
                "bool": {
                    "must": [
                        {"term": {"resource_id": resource_id}},
                        {
                            "range": {
                                "chunk_no": {
                                    "gte": chunk_no - _NEIGHBOR_WINDOW,
                                    "lte": chunk_no + _NEIGHBOR_WINDOW,
                                }
                            }
                        },
                    ]
                }
            }
        )
    if not should_clauses:
        return {}
    body: dict[str, Any] = {
        "query": {"bool": {"should": should_clauses, "minimum_should_match": 1}}
    }
    scope_filter = _owner_scope_filter(owner_scope)
    if scope_filter:
        body["query"]["bool"]["filter"] = scope_filter
    return body


def _clean_neighbor_hits(raw: dict) -> list[tuple[int, int, dict]]:
    """Extract (resource_id, chunk_no, preview) triples from a neighbor query."""
    entries: list[tuple[int, int, dict]] = []
    for hit in raw.get("hits", {}).get("hits", []):
        source = hit.get("_source", {})
        resource_id = source.get("resource_id")
        chunk_no = source.get("chunk_no")
        if resource_id is None or chunk_no is None:
            continue
        entries.append(
            (
                resource_id,
                chunk_no,
                {
                    "chunk_no": chunk_no,
                    "title": source.get("title", ""),
                    "chunk_text_preview": (source.get("chunk_text") or "")[
                        :_NEIGHBOR_PREVIEW_CHARS
                    ],
                },
            )
        )
    return entries


def _extract_total(raw: dict) -> int:
    total = raw.get("hits", {}).get("total", {})
    if isinstance(total, dict):
        return int(total.get("value", 0) or 0)
    return int(total or 0)


def _clean_hit(hit: dict, include_annotations: bool = False) -> dict:
    """Convert one raw ES hit into the LLM-friendly hit shape."""
    source = hit.get("_source", {})
    entry: dict[str, Any] = {}

    for field in _INCLUDE_FIELDS:
        if field in source:
            entry[field] = source[field]

    # Fused hits carry _rrf (client-side RRF); raw hits carry _score.
    score = hit.get("_rrf")
    if score is None:
        score = hit.get("_score")
    if score is not None:
        entry["_score"] = round(score, 4) if isinstance(score, float) else score

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
    return entry


def _clean_hits(hits_list: list[dict], include_annotations: bool = False) -> list[dict]:
    return [_clean_hit(hit, include_annotations) for hit in hits_list]


def _attach_neighbors(hits: list[dict], entries: list[tuple[int, int, dict]]) -> None:
    """Attach each hit's within-window neighbor chunks (in place).

    *entries* come from a single neighbor query covering every hit's window,
    so attachment must re-apply the +/-_NEIGHBOR_WINDOW bound — otherwise a
    hit inherits neighbors fetched for other hits of the same resource.
    """
    for hit in hits:
        resource_id = hit.get("resource_id")
        chunk_no = hit.get("chunk_no")
        if resource_id is None or chunk_no is None:
            continue
        seen: set[int] = set()
        neighbors: list[dict] = []
        for e_rid, e_cno, entry in entries:
            if (
                e_rid == resource_id
                and e_cno != chunk_no
                and abs(e_cno - chunk_no) <= _NEIGHBOR_WINDOW
                and e_cno not in seen
            ):
                seen.add(e_cno)
                neighbors.append(entry)
        if neighbors:
            neighbors.sort(key=lambda e: e["chunk_no"])
            hit["neighbors"] = neighbors


def _clean_response(raw: dict, include_annotations: bool = False) -> dict[str, Any]:
    """Clean raw ES response into LLM-friendly format."""
    hits_raw = raw.get("hits", {})
    return {
        "total": _extract_total(raw),
        "took_ms": raw.get("took", 0),
        "hits": _clean_hits(hits_raw.get("hits", []), include_annotations),
    }


class SearchDocumentsTool:
    """LLM-friendly document chunk search via Elasticsearch.

    Translates semantic query parameters into ES Query DSL internally
    and returns cleaned, structured results stripped of ES internals.
    """

    name: str = "search_documents"
    display_name: str | None = "搜索文档"
    description: str = (
        "在已索引的文档块中搜索内容。只需传入搜索关键词，系统会自动构建查询并格式化结果。"
        '用引号包裹的词会作为精确短语匹配（如 "安全生产" 匹配完整短语），'
        "其余部分需大部分关键词命中（含完整短语的文档排名更靠前）。"
        "支持按文档ID、文档类型、标签过滤。"
        "词法模式下 total 为精确总数（total_mode=exact）；"
        "hybrid 模式下 total 为窗口内融合候选数（total_mode=window）。"
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "搜索关键词或短语。中英文引号包裹的文本做精确短语匹配；"
                    "其余文本要求大部分关键词命中，含完整短语的文档块排名更靠前。"
                    '示例：关于"安全生产"的通知'
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
                "description": (
                    "跳过的结果数，用于分页，默认 0。hybrid/重排模式下翻页深度受融合窗口"
                    "上限（默认 200）约束，超深分页返回空页，请改用 document_id/doc_type/tags"
                    " 过滤缩小范围"
                ),
            },
            "limit": {
                "type": "integer",
                "description": "返回的最大结果数，默认 10",
            },
            "rerank": {
                "type": "boolean",
                "description": (
                    "是否用 LLM 对粗排前 50 条做相关性重排后再截取返回，默认 false；"
                    "重排失败时保持原顺序"
                ),
            },
            "use_time_decay": {
                "type": "boolean",
                "description": "是否按发布日期做时间衰减加权（新文档优先），默认 false",
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
        rerank = bool(kwargs.pop("rerank", False))
        use_time_decay = bool(kwargs.get("use_time_decay", False))

        # TTL cache: coarse results are reusable within a short window.
        # Rerank always recomputes (LLM nondeterminism); on a non-rerank
        # cache hit the stored entry already includes neighbors.
        cache_key = _cache_key(rerank=rerank, **kwargs)
        cached = _cache_get(cache_key)
        cleaned: dict[str, Any] | None = None
        if cached is not None:
            cleaned = copy.deepcopy(cached)
            cleaned["cached"] = True

        query_vector: list[float] | None = None
        # Set when the fetch used the window policy (embedding configured or
        # rerank): such results are sliced in-process, including the embed-
        # degraded fallback where query_vector is None but the lexical arm
        # already fetched a window instead of the requested page.
        hybrid_planned = False
        if cleaned is None:
            # Pure config check (no network) — decides both the fetch window
            # policy and where time decay is applied (server-side gauss only
            # on the pure lexical path; hybrid plans decay client-side on
            # the fused scores so both arms decay consistently).
            embed_planned = False
            try:
                from embeddings import embedding_config

                embed_planned = embedding_config() is not None
            except Exception:
                logger.warning("embedding config check failed; lexical only", exc_info=True)
            hybrid_planned = embed_planned

            try:
                es_body = _build_es_query(
                    query=query,
                    document_id=kwargs.get("document_id"),
                    doc_type=kwargs.get("doc_type"),
                    tags=kwargs.get("tags"),
                    search_fields=kwargs.get("search_fields"),
                    owner_scope=kwargs.get("_owner_scope", _QUERY_UNSET),
                    use_time_decay=use_time_decay and not hybrid_planned,
                )
            except Exception as exc:
                return ToolResult(success=False, error=f"查询构建失败: {exc}")

        if cleaned is None:
            try:
                from es_client import search_chunks

                # Fetch-window policy: hybrid/rerank pages slice an
                # in-process fused candidate list, so the lexical arm must
                # fetch the whole window (skip+limit) — fetching just the
                # first page would keep lexical hits beyond page 1 out of
                # fusion.  Pure lexical keeps ES-native paging (exact
                # totals, deep paging up to _MAX_SKIP).  hybrid_planned was
                # decided above (pure config check): when embedding is
                # configured the window fetch applies even if the embed
                # call later fails, and the degraded result is sliced.
                window = min(max(skip + limit, _KNN_K), _MAX_WINDOW)
                if rerank:
                    lex_skip, lex_limit = 0, min(window, _RERANK_MAX_FETCH)
                elif hybrid_planned:
                    lex_skip, lex_limit = 0, window
                else:
                    lex_skip, lex_limit = skip, limit

                # The lexical fetch and the query embedding (when planned)
                # run concurrently — the kNN arm depends on the vector and
                # is issued after both resolve.  Any embedding failure
                # degrades to lexical-only search.
                lex_task = asyncio.to_thread(
                    search_chunks,
                    query_body=es_body,
                    skip=lex_skip,
                    limit=lex_limit,
                )
                if hybrid_planned:
                    try:
                        from embeddings import embed_query
                    except Exception:
                        logger.warning("embedding import failed; lexical only", exc_info=True)
                        raw = await lex_task
                    else:
                        raw, query_vector = await asyncio.gather(
                            lex_task, embed_query(query), return_exceptions=True
                        )
                        if isinstance(query_vector, Exception):
                            logger.warning(
                                "query embedding failed; falling back to lexical",
                                exc_info=query_vector,
                            )
                            query_vector = None
                        if isinstance(raw, BaseException):
                            raise raw
                else:
                    raw = await lex_task
            except Exception as exc:
                return ToolResult(success=False, error=str(exc))

            if query_vector is not None:
                # Hybrid: fuse the lexical result with a parallel kNN result
                # (client-side RRF — ES rank.rrf needs a commercial license).
                # A failing kNN arm degrades to the lexical result.  Both
                # arms fetch the same window; the fused total is the number
                # of candidates inside that window (total_mode=window), not
                # an exact corpus count.
                try:
                    filters = _build_filters(
                        document_id=kwargs.get("document_id"),
                        doc_type=kwargs.get("doc_type"),
                        tags=kwargs.get("tags"),
                        owner_scope=kwargs.get("_owner_scope", _QUERY_UNSET),
                    )
                    knn_k = max(_KNN_K, window)
                    knn_body = _build_knn_query(query_vector, filters, k=knn_k)
                    from es_client import search_chunks as _search_chunks

                    raw_knn = await asyncio.to_thread(
                        _search_chunks, query_body=knn_body, skip=0, limit=knn_k
                    )
                    # Recency weighting on fused scores keeps both arms
                    # consistent (server-side gauss cannot reach the kNN arm).
                    decay_fn = None
                    if use_time_decay:
                        today = date.today()

                        def _decay(hit: dict) -> float:
                            return _decay_multiplier(
                                (hit.get("_source", {}) or {}).get("publish_date"), today
                            )

                        decay_fn = _decay
                    fused = _rrf_fuse(
                        raw.get("hits", {}).get("hits", []),
                        raw_knn.get("hits", {}).get("hits", []),
                        decay_fn=decay_fn,
                    )
                    cleaned = {
                        "total": len(fused),
                        "total_mode": "window",
                        "took_ms": raw.get("took", 0) + raw_knn.get("took", 0),
                        "hits": _clean_hits(
                            fused,
                            include_annotations=bool(kwargs.get("include_annotations", False)),
                        ),
                        "mode": "hybrid",
                    }
                    if use_time_decay:
                        cleaned["time_decay_applied"] = True
                except Exception:
                    logger.warning("kNN arm failed; falling back to lexical", exc_info=True)
                    cleaned = _clean_response(
                        raw,
                        include_annotations=bool(kwargs.get("include_annotations", False)),
                    )
                    cleaned["mode"] = "lexical"
                    cleaned["total_mode"] = "exact"
                    if use_time_decay:
                        # The lexical body was built without server-side
                        # gauss (hybrid planned client-side decay); losing
                        # the vector loses decay for this call.
                        cleaned["time_decay_applied"] = False
                        logger.warning("time decay skipped: kNN arm failed")
            else:
                cleaned = _clean_response(
                    raw,
                    include_annotations=bool(kwargs.get("include_annotations", False)),
                )
                cleaned["mode"] = "lexical"
                cleaned["total_mode"] = "exact"
                if use_time_decay:
                    # True decay on the pure lexical path (server-side gauss
                    # in the body); False when a planned hybrid degraded and
                    # the body was built without gauss.
                    applied = not hybrid_planned
                    cleaned["time_decay_applied"] = applied
                    if not applied:
                        logger.warning("time decay skipped: embedding failed, degraded lexical")

            # Coarse result (pre-rerank, pre-neighbors) is what the cache
            # stores for rerank calls.
            if rerank:
                _cache_put(cache_key, copy.deepcopy(cleaned))

        # Optional LLM listwise rerank: reorder the rough top-N, then slice.
        # Failures keep the original order — reranking is best-effort.
        # ``rerank_partial`` marks that reordering did not fully apply
        # (model omitted candidates, or rerank failed outright).
        if rerank:
            try:
                from rerank import rerank_hits

                ordered, partial = await rerank_hits(query, cleaned["hits"][:_RERANK_MAX_FETCH])
                cleaned["hits"] = ordered[skip : skip + limit]
                cleaned["reranked"] = True
                cleaned["rerank_partial"] = partial
            except Exception:
                logger.warning("rerank failed; keeping original order", exc_info=True)
                cleaned["hits"] = cleaned["hits"][skip : skip + limit]
                cleaned["rerank_partial"] = True
        elif hybrid_planned:
            # Window fetch (hybrid planned or degraded): slice in-process.
            # Covers both the fused result and the embed-failure fallback.
            cleaned["hits"] = cleaned["hits"][skip : skip + limit]

        # Neighbor context expansion: attach chunks adjacent to each hit so
        # provisions spanning chunk boundaries come back as a unit.  Best
        # effort — failures degrade to hits without neighbors.  Skipped on
        # non-rerank cache hits (the stored entry already has them).
        from_cache = cached is not None
        if rerank or not from_cache:
            hits = cleaned.get("hits") or []
            if hits:
                try:
                    from es_client import search_chunks

                    owner_scope = kwargs.get("_owner_scope", _QUERY_UNSET)
                    neighbor_query = _build_neighbor_query(hits, owner_scope)
                    if neighbor_query:
                        neighbor_raw = await asyncio.to_thread(
                            search_chunks,
                            query_body=neighbor_query,
                            skip=0,
                            limit=len(hits) * (_NEIGHBOR_WINDOW * 2 + 1) + 2,
                        )
                        entries = _clean_neighbor_hits(neighbor_raw)
                        _attach_neighbors(hits, entries)
                except Exception:
                    logger.warning("neighbor expansion failed", exc_info=True)

        # Cache the final result (with neighbors) on the non-rerank path.
        if not rerank and not from_cache:
            _cache_put(cache_key, copy.deepcopy(cleaned))

        return ToolResult(success=True, data=cleaned)


def create_search_documents_tool() -> SearchDocumentsTool:
    """Factory function for backward compatibility with existing imports."""
    return SearchDocumentsTool()


#: read_chunks guards: coordinate count, per-chunk and total response size.
_READ_MAX_CHUNKS = 10
_READ_CHUNK_MAX_CHARS = 3_000
_READ_TOTAL_MAX_CHARS = 30_000


class ReadChunksTool:
    """Coordinate-based full chunk retrieval for search_documents hits.

    Hits and neighbor previews are truncated for context economy; this tool
    reads complete chunk text back by (resource_id, chunk_no) coordinate so
    provisions spanning chunk boundaries can be quoted in full.  Subject to
    the same host-injected owner-scope visibility as search_documents.
    """

    name: str = "read_chunks"
    display_name: str | None = "读取文档块"
    description: str = (
        "按 resource_id+chunk_no 坐标取回完整文档块原文（含跨块条款续文）。"
        "坐标来自 search_documents 结果的 hits/neighbors 字段。一次最多 10 个坐标。"
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "chunks": {
                "type": "array",
                "maxItems": 10,
                "items": {
                    "type": "object",
                    "properties": {
                        "resource_id": {"type": "integer"},
                        "chunk_no": {"type": "integer"},
                    },
                    "required": ["resource_id", "chunk_no"],
                },
                "description": "要读取的块坐标列表，最多 10 个",
            },
            "with_neighbors": {
                "type": "boolean",
                "description": "同时返回每个坐标 ±1 相邻块，默认 false",
            },
        },
        "required": ["chunks"],
    }
    #: Full text is the point of this tool; results stay inline (no $ref
    #: persistence) so the model can actually read them back.
    skip_persist: bool = True

    async def execute(self, **kwargs: Any) -> ToolResult:
        raw_chunks = kwargs.get("chunks")
        if not isinstance(raw_chunks, list) or not raw_chunks:
            return ToolResult(success=False, error="chunks 必须是非空坐标数组")
        if len(raw_chunks) > _READ_MAX_CHUNKS:
            return ToolResult(success=False, error=f"一次最多读取 {_READ_MAX_CHUNKS} 个块")

        coords: list[tuple[int, int]] = []
        seen: set[tuple[int, int]] = set()
        for item in raw_chunks:
            if not isinstance(item, dict):
                continue
            rid, cno = item.get("resource_id"), item.get("chunk_no")
            if isinstance(rid, bool) or isinstance(cno, bool):
                continue
            if not isinstance(rid, int) or not isinstance(cno, int):
                continue
            if (rid, cno) not in seen:
                seen.add((rid, cno))
                coords.append((rid, cno))
        if not coords:
            return ToolResult(
                success=False, error="坐标无效：需要 resource_id 与 chunk_no 整数字段"
            )

        with_neighbors = bool(kwargs.get("with_neighbors", False))
        should: list[dict] = []
        for rid, cno in coords:
            chunk_clause: dict[str, Any] = (
                {"range": {"chunk_no": {"gte": cno - 1, "lte": cno + 1}}}
                if with_neighbors
                else {"term": {"chunk_no": cno}}
            )
            should.append({"bool": {"must": [{"term": {"resource_id": rid}}, chunk_clause]}})
        body: dict[str, Any] = {
            "query": {"bool": {"should": should, "minimum_should_match": 1}}
        }
        # Same visibility semantics as the main search — filtered-out
        # chunks simply land in `missing` (no exists/permission distinction,
        # avoiding existence leaks).
        scope_filter = _owner_scope_filter(kwargs.get("_owner_scope", _QUERY_UNSET))
        if scope_filter:
            body["query"]["bool"]["filter"] = scope_filter

        try:
            from es_client import search_chunks

            raw = await asyncio.to_thread(
                search_chunks,
                query_body=body,
                skip=0,
                limit=len(coords) * 3 + 2,
            )
        except Exception as exc:
            return ToolResult(success=False, error=str(exc))

        found: dict[tuple[int, int], dict[str, Any]] = {}
        total_chars = 0
        truncated = False
        for hit in raw.get("hits", {}).get("hits", []):
            src = hit.get("_source", {}) or {}
            rid, cno = src.get("resource_id"), src.get("chunk_no")
            if rid is None or cno is None:
                continue
            text = src.get("chunk_text") or ""
            if len(text) > _READ_CHUNK_MAX_CHARS:
                text = text[:_READ_CHUNK_MAX_CHARS] + "…"
                truncated = True
            remaining = _READ_TOTAL_MAX_CHARS - total_chars
            if len(text) > remaining:
                text = text[: max(remaining, 0)] + "…"
                truncated = True
            entry = {
                "resource_id": rid,
                "chunk_no": cno,
                "title": src.get("title", ""),
                "doc_type": src.get("doc_type", ""),
                "publish_date": src.get("publish_date"),
                "chunk_text": text,
            }
            total_chars += len(entry["chunk_text"])
            found[(rid, cno)] = entry
            if truncated and total_chars >= _READ_TOTAL_MAX_CHARS:
                break

        # Requested coordinates keep their order; neighbor extras follow,
        # sorted deterministically.
        chunks_out = [found[c] for c in coords if c in found]
        if with_neighbors:
            requested = set(coords)
            chunks_out.extend(found[c] for c in sorted(found) if c not in requested)
        missing = [
            {"resource_id": rid, "chunk_no": cno} for rid, cno in coords if (rid, cno) not in found
        ]
        data: dict[str, Any] = {"chunks": chunks_out}
        if missing:
            data["missing"] = missing
        if truncated:
            data["truncated"] = True
        return ToolResult(success=True, data=data)
