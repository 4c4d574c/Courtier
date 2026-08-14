"""Host-side embedding client for resource ingestion (OpenAI-compatible).

Plugins cannot import the host app package, so the search plugin carries its
own client in ``plugins/shared/search/embeddings.py``; this module serves the
host's ingest path (resource_service / reindex script) against the same
endpoint.  Failures degrade to lexical-only chunks (None vector entries).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 30.0


def embedding_enabled(settings: Any) -> bool:
    """True when both the LLM base URL and an embedding model are configured.

    Defensive getattr keeps mocked settings objects (tests, partial configs)
    from failing the check."""
    return bool(
        getattr(settings, "llm_base_url", "") and getattr(settings, "llm_embedding_model", "")
    )


async def embed_chunks(settings: Any, texts: list[str]) -> list[list[float] | None]:
    """Embed *texts* in batches; each entry is None when embedding is
    disabled or that batch failed, so callers stay lexical-only per chunk."""
    if not embedding_enabled(settings) or not texts:
        return [None] * len(texts)

    url = f"{settings.llm_base_url.rstrip('/')}/embeddings"
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if settings.llm_api_key:
        headers["Authorization"] = f"Bearer {settings.llm_api_key}"

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
