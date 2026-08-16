"""LLM listwise reranker for search hits (OpenAI-compatible chat completions).

Reads the LLM endpoint from the process environment (injected by the host
from plugin.yaml runtime.env). Any failure raises — the caller keeps the
original order, so reranking is always a best-effort improvement.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from tools import _parse_query

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 30.0
#: Total char budget across all candidates in one listwise prompt; each
#: candidate gets budget//n chars, clamped to [min, max].  50 candidates
#: ≈ 480 chars each; short candidate lists get richer evidence.
_CANDIDATE_BUDGET_CHARS = 24_000
_CANDIDATE_MIN_CHARS = 240
_CANDIDATE_MAX_CHARS = 800
_ORDER_RE = re.compile(r"\[[\d,\s]*\]")
#: Sentence boundaries for official-document prose (declarations end with
#: 。/；, items with newlines); zero-width lookbehind keeps delimiters.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。；！？\n])")
_QUERY_NOISE_RE = re.compile(r"[\s，。；、！？·\"'\u201c\u201d\u2018\u2019「』『』]")


def _llm_env() -> tuple[str, str, str] | None:
    """Return (base_url, api_key, model) or None when not configured."""
    base_url = os.environ.get("LLM_IP", "").strip()
    model = os.environ.get("LLM_NAME", "").strip()
    if not base_url or not model:
        return None
    return base_url, os.environ.get("LLM_API_KEY", "").strip(), model


def _candidate_chars(n: int) -> int:
    """Per-candidate evidence length: total budget split over *n* candidates."""
    budget = _CANDIDATE_BUDGET_CHARS // max(n, 1)
    return min(_CANDIDATE_MAX_CHARS, max(_CANDIDATE_MIN_CHARS, budget))


def _query_grams(query: str) -> set[str]:
    """Lexical matching units for segment localization: quoted phrases
    whole, free text as character bigrams (mirrors the bigram tokenization
    the lexical search arm relies on)."""
    phrases, free = _parse_query(query)
    grams = {p for p in phrases if p}
    cleaned = _QUERY_NOISE_RE.sub("", free)
    grams |= {cleaned[i : i + 2] for i in range(len(cleaned) - 1)}
    return grams


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


def _prompt(query: str, hits: list[dict]) -> str:
    grams = _query_grams(query)
    chars = _candidate_chars(len(hits))
    lines = []
    for i, hit in enumerate(hits, start=1):
        title = hit.get("title", "") or "（无标题）"
        text = _best_window(grams, hit.get("chunk_text") or "", chars).replace("\n", " ")
        lines.append(f"{i}. 《{title}》 {text}")
    return (
        "你是检索结果重排器。根据查询与候选文档块的相关性，输出候选编号的排序列表"
        "（JSON 数组，最相关的在前，编号从 1 开始，必须包含所有编号）。\n"
        "候选为原文节选，省略号表示截断，节选中心为与查询最相关的段落。\n"
        f"查询：{query}\n候选文档块：\n" + "\n".join(lines) + "\n"
        "只输出 JSON 数组，如 [3, 1, 2]。"
    )


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


async def rerank_hits(query: str, hits: list[dict]) -> tuple[list[dict], bool]:
    """Return (*hits* reordered, partial).

    Unranked candidates are appended in original order so a partial model
    answer never silently drops hits.  Coverage below half the candidates is
    treated as an untrustworthy ordering and keeps the original order
    entirely.  Raises on configuration/request/parse failure; the caller
    keeps the original order in that case.
    """
    if len(hits) <= 1:
        return hits, False
    env = _llm_env()
    if env is None:
        raise RuntimeError("LLM_IP/LLM_NAME not configured for rerank")
    base_url, api_key, model = env

    import httpx

    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": "你是检索结果重排器，只输出 JSON 数组。"},
            {"role": "user", "content": _prompt(query, hits)},
        ],
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
        resp = await client.post(
            f"{base_url.rstrip('/')}/chat/completions", json=payload, headers=headers
        )
        resp.raise_for_status()
        content = (resp.json().get("choices") or [{}])[0].get("message", {}).get("content", "")

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
