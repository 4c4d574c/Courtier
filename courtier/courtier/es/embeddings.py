"""Host-side embedding client (OpenAI-compatible embeddings API).

Single source for both retrieval sides: index-time chunk embedding
(resource_service / reindex script) and query-time embedding injected into
the search tool at the dispatch boundary (see
courtier.agent.tools.param_injection).  Failures degrade to lexical-only
(None vector entries / unset injected parameter).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 30.0


def _effective_base_url(settings: Any) -> str:
    """Embedding endpoint URL — the dedicated setting, nothing else.

    Embedding shares nothing with the chat LLM endpoint: the two may live
    on different hosts, and the chat model pool never affects it.  Unset
    URL = vector retrieval off (callers degrade to lexical-only)."""
    return getattr(settings, "llm_embedding_base_url", "") or ""


def _effective_api_key(settings: Any) -> str:
    return getattr(settings, "llm_embedding_api_key", "") or ""


def embedding_enabled(settings: Any) -> bool:
    """True when the dedicated embedding endpoint URL and an embedding
    model are both configured — embedding never falls back to the chat
    LLM endpoint or key.

    Defensive getattr keeps mocked settings objects (tests, partial configs)
    from failing the check."""
    return bool(_effective_base_url(settings) and getattr(settings, "llm_embedding_model", ""))


async def embed_chunks(settings: Any, texts: list[str]) -> list[list[float] | None]:
    """Embed *texts* in batches; each entry is None when embedding is
    disabled or that batch failed, so callers stay lexical-only per chunk."""
    if not embedding_enabled(settings) or not texts:
        return [None] * len(texts)

    url = f"{_effective_base_url(settings).rstrip('/')}/embeddings"
    headers: dict[str, str] = {"Content-Type": "application/json"}
    api_key = _effective_api_key(settings)
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    vectors: list[list[float] | None] = [None] * len(texts)
    batch_size = max(1, settings.llm_embedding_batch_size)
    async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
        for start in range(0, len(texts), batch_size):
            batch = texts[start : start + batch_size]
            try:
                resp = await client.post(
                    url,
                    json={"model": settings.llm_embedding_model, "input": batch},
                    headers=headers,
                )
                resp.raise_for_status()
                data = resp.json().get("data")
                if not isinstance(data, list):
                    continue
                items = sorted(data, key=lambda d: d.get("index", 0))
                for offset, item in enumerate(items):
                    if isinstance(item, dict) and "embedding" in item:
                        vectors[start + offset] = item["embedding"]
            except Exception:
                logger.warning(
                    "embedding batch failed (%d texts); chunks stay lexical-only",
                    len(batch),
                    exc_info=True,
                )
    return vectors


async def embed_query(settings: Any, text: str) -> list[float] | None:
    """Embed a single query text; None when disabled or the call failed.

    Used by the tool-dispatch boundary to inject the query vector into the
    search tool (host-injected parameter), so query-time and index-time
    embeddings share one client and one configuration."""
    if not text.strip():
        return None
    vectors = await embed_chunks(settings, [text])
    return vectors[0] if vectors else None
