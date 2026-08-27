"""Host-side listwise rerank for search tool results.

Migrated from the search plugin (plugins/shared/search/rerank.py) so the
plugin is a pure ES retrieval primitive: when the model calls the search
tool with ``rerank=true``, the host reorders the returned hits with one
chat-completion call against the main model, at the registry boundary
and before persistence (ToolRegistry result post-processor).  Rerank is
always best-effort: any failure keeps the original order and marks the
result ``rerank_partial``.

Prompt text lives in the core default templates (``search.rerank.system``
/ ``search.rerank.user``) — no hardcoded NL in this module.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable

from courtier.agent.core.backends.openai_backend import OpenAIModelBackend
from courtier.agent.core.protocol import ChatMessage, ChatRequest
from courtier.agent.tools.protocol import ToolResult

logger = logging.getLogger(__name__)


def _noop_progress(_progress: Any) -> None:
    """Progress sink for the finalize re-dispatch (no SSE to fan out)."""

_TIMEOUT_SECONDS = 30.0
#: Per-candidate evidence clamp: 50 candidates ≈ 480 chars each; short
#: candidate lists get richer evidence (budget split, see _candidate_chars).
_CANDIDATE_MIN_CHARS = 240
_CANDIDATE_MAX_CHARS = 800
_ORDER_RE = re.compile(r"\[[\d,\s]*\]")
#: Sentence boundaries for official-document prose (declarations end with
#: 。/；, items with newlines); zero-width lookbehind keeps delimiters.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。；！？\n])")
_QUERY_NOISE_RE = re.compile(r"[\s，。；、！？·\"'\u201c\u201d\u2018\u2019「』『』]")
_PHRASE_RE = re.compile(
    r"[\u0022\u201c\u201d\u2018\u2019\u300c\u300d]"
    r"(.+?)"
    r"[\u0022\u201c\u201d\u2018\u2019\u300c\u300d]"
)


def _candidate_chars(n: int, budget_chars: int) -> int:
    """Per-candidate evidence length: total budget split over *n* candidates."""
    budget = budget_chars // max(n, 1)
    return min(_CANDIDATE_MAX_CHARS, max(_CANDIDATE_MIN_CHARS, budget))


def _query_grams(query: str) -> set[str]:
    """Lexical matching units for segment localization: quoted phrases
    whole, free text as character bigrams (mirrors the bigram tokenization
    the lexical search arm relies on)."""
    grams = {m.group(1) for m in _PHRASE_RE.finditer(query)}
    free = _PHRASE_RE.sub(" ", query)
    cleaned = _QUERY_NOISE_RE.sub("", free)
    grams |= {cleaned[i : i + 2] for i in range(len(cleaned) - 1)}
    return {g for g in grams if g}


def _best_window(grams: set[str], text: str, chars: int) -> str:
    """A *chars*-wide window of *text* centered on the most relevant sentence.

    Scores each sentence by query-gram overlap and centers the window on
    the best one (with a quarter window of leading context).  Falls back
    to the head of the text when nothing overlaps (e.g. pure kNN hits
    whose relevance is semantic, not lexical).  Ellipses mark truncation.
    """
    if len(text) <= chars:
        return text
    best_score, best_start, offset = 0, 0, 0
    for sent in _SENTENCE_SPLIT_RE.split(text):
        if sent.strip():
            score = sum(1 for g in grams if g in sent)
            if score > best_score:
                best_score, best_start = score, offset
        offset += len(sent)
    if best_score == 0:
        return text[:chars]
    start = max(0, best_start - chars // 4)
    end = min(len(text), start + chars)
    start = max(0, end - chars)
    prefix, suffix = ("…" if start > 0 else ""), ("…" if end < len(text) else "")
    return prefix + text[start:end] + suffix


def _parse_order(content: str, n: int) -> list[int] | None:
    """Parse a model output like ``[3,1,2]``, tolerating prose around it."""
    data: Any
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        match = _ORDER_RE.search(content)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    if not isinstance(data, list):
        return None
    order: list[int] = []
    for item in data:
        try:
            num = int(item)
        except (TypeError, ValueError):
            continue
        if 1 <= num <= n and num not in order:
            order.append(num)
    return order or None


def _candidate_lines(query: str, hits: list[dict], budget_chars: int) -> list[str]:
    grams = _query_grams(query)
    chars = _candidate_chars(len(hits), budget_chars)
    lines = []
    for i, hit in enumerate(hits, start=1):
        title = hit.get("title", "") or "（无标题）"
        text = _best_window(grams, hit.get("chunk_text") or "", chars).replace("\n", " ")
        lines.append(f"{i}. 《{title}》 {text}")
    return lines


async def rerank_hits(
    query: str,
    hits: list[dict],
    *,
    settings: Any,
    prompt_engine: Any,
) -> tuple[list[dict], bool]:
    """Return (*hits* reordered, partial).

    Unranked candidates are appended in original order so a partial model
    answer never silently drops hits.  Coverage below half the candidates
    is treated as an untrustworthy ordering and keeps the original order
    entirely.  Raises on configuration/request/parse failure; the caller
    keeps the original order in that case.
    """
    if len(hits) <= 1 or not query.strip():
        return hits, False
    if not settings.llm_base_url or not settings.llm_model:
        raise RuntimeError("llm_base_url/llm_model not configured for rerank")

    budget = settings.search_rerank_candidate_budget_chars
    system = prompt_engine.render("search.rerank.system")
    user = prompt_engine.render(
        "search.rerank.user",
        query=query,
        candidates="\n".join(_candidate_lines(query, hits, budget)),
    )

    # One-shot utility call through the host model client: rerank is
    # occasional, so a per-call backend beats lifecycle management and
    # keeps this path independent of the conversational agent loop.
    backend = OpenAIModelBackend(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        model=settings.llm_model,
        temperature=0.0,
        timeout=_TIMEOUT_SECONDS,
    )
    try:
        response = await backend.chat(
            ChatRequest(
                model=settings.llm_model,
                messages=(
                    ChatMessage(role="system", content=system),
                    ChatMessage(role="user", content=user),
                ),
                temperature=0.0,
                metadata={"extra_body": {"response_format": {"type": "json_object"}}},
            )
        )
    finally:
        await backend.close()

    content = response.message.content or ""
    order = _parse_order(content, len(hits))
    if order is None:
        raise RuntimeError(f"unparsable rerank output: {content[:80]!r}")
    by_index = {i + 1: hit for i, hit in enumerate(hits)}
    ordered = [by_index[num] for num in order]
    ranked = set(order)
    remainder = [hit for i, hit in enumerate(hits, 1) if i not in ranked]
    if len(order) * 2 < len(hits):
        return list(hits), True
    return ordered + remainder, bool(remainder)


def make_search_rerank_post_processor(
    prompt_engine: Any,
    tool_registry: Any = None,
) -> Callable:
    """Build the ToolRegistry result post-processor for search rerank.

    Applies to successful ``search_documents`` results whose call args had
    ``rerank=true``.  The plugin returns the coarse top-N candidates
    (pre-pagination, pre-neighbor-expansion); this processor reorders the
    top ``search_rerank_fetch`` with one model call, then hands the
    ordered payload back to the tool's *finalize* mode (``finalize``
    kwarg) so the plugin applies pagination and neighbor expansion —
    both need ES access and stay plugin-side.  Failures keep the
    original order (still finalized) and mark ``rerank_partial``.
    """

    async def _finalize(tool: Any, data: dict[str, Any], kwargs: dict[str, Any]) -> Any:
        return await tool.execute(
            on_progress=_noop_progress,
            query=kwargs.get("query", ""),
            skip=kwargs.get("skip", 0),
            limit=kwargs.get("limit", 10),
            finalize=data,
        )

    async def _post_process(tool_name: str, kwargs: dict[str, Any], tool_result: Any):
        if tool_name != "search_documents":
            return None
        if not kwargs.get("rerank"):
            return None
        if not isinstance(tool_result, ToolResult) or not tool_result.success:
            return None
        data = tool_result.data
        if not isinstance(data, dict) or not isinstance(data.get("hits"), list):
            return None

        from courtier.config import get_settings

        settings = get_settings()
        fetch = max(1, settings.search_rerank_fetch)
        head = data["hits"][:fetch]
        tail = data["hits"][fetch:]
        try:
            reordered, partial = await rerank_hits(
                kwargs.get("query", ""),
                head,
                settings=settings,
                prompt_engine=prompt_engine,
            )
            reranked = True
        except Exception:
            logger.warning("search rerank failed; keeping original order", exc_info=True)
            reordered, partial, reranked = list(head), True, False

        finalize_data = {
            **data,
            "hits": reordered + tail,
            "reranked": reranked,
            "rerank_partial": partial,
        }
        if tool_registry is None:
            return tool_result.model_copy(update={"data": finalize_data})

        try:
            tool = tool_registry.get(tool_name)
            final = await _finalize(tool, finalize_data, kwargs)
        except Exception:
            logger.warning(
                "search rerank finalize failed; returning reranked coarse list",
                exc_info=True,
            )
            return tool_result.model_copy(update={"data": finalize_data})
        if isinstance(final, ToolResult) and final.success:
            return final
        logger.warning("search rerank finalize returned failure; using coarse list")
        return tool_result.model_copy(update={"data": finalize_data})

    return _post_process
