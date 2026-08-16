"""LLM listwise reranker for search hits (OpenAI-compatible chat completions).

Reads the LLM endpoint from the process environment (injected by the host
from plugin.yaml runtime.env).  Any failure raises — the caller keeps the
original order, so reranking is always a best-effort improvement.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 30.0
_CANDIDATE_CHARS = 200
_ORDER_RE = re.compile(r"\[[\d,\s]*\]")


def _llm_env() -> tuple[str, str, str] | None:
    """Return (base_url, api_key, model) or None when not configured."""
    base_url = os.environ.get("LLM_IP", "").strip()
    model = os.environ.get("LLM_NAME", "").strip()
    if not base_url or not model:
        return None
    return base_url, os.environ.get("LLM_API_KEY", "").strip(), model


def _prompt(query: str, hits: list[dict]) -> str:
    lines = []
    for i, hit in enumerate(hits, start=1):
        title = hit.get("title", "") or "（无标题）"
        text = (hit.get("chunk_text") or "")[:_CANDIDATE_CHARS].replace("\n", " ")
        lines.append(f"{i}. 《{title}》 {text}")
    return (
        "你是检索结果重排器。根据查询与候选文档块的相关性，输出候选编号的排序列表"
        "（JSON 数组，最相关的在前，编号从 1 开始，必须包含所有编号）。\n"
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
