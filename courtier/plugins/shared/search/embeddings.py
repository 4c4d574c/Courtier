"""Embedding client for the search plugin (OpenAI-compatible /embeddings).

Reads the LLM endpoint from the process environment (injected by the host
from plugin.yaml runtime.env).  Cleanly unavailable when no embedding model
is configured, so lexical search keeps working without vectors.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 30.0
_DEFAULT_BATCH_SIZE = 25


class EmbeddingUnavailable(RuntimeError):
    """Embedding endpoint/model not configured or unreachable."""


def embedding_config() -> tuple[str, str, str] | None:
    """Return (base_url, api_key, model) or None when not configured."""
    model = os.environ.get("LLM_EMBEDDING_NAME", "").strip()
    base_url = os.environ.get("LLM_IP", "").strip()
    if not model or not base_url:
        return None
    return base_url, os.environ.get("LLM_API_KEY", "").strip(), model


def _embedding_batch_size() -> int:
    try:
        return max(1, int(os.environ.get("LLM_EMBEDDING_BATCH_SIZE", str(_DEFAULT_BATCH_SIZE))))
    except ValueError:
        return _DEFAULT_BATCH_SIZE


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed *texts* in batches via the OpenAI-compatible embeddings API.

    Raises :class:`EmbeddingUnavailable` when the endpoint/model is not
    configured or any request fails after one retry.
    """
    config = embedding_config()
    if config is None:
        raise EmbeddingUnavailable("LLM_EMBEDDING_NAME/LLM_IP not configured")
    base_url, api_key, model = config
    if not texts:
        return []

    import httpx

    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    url = f"{base_url.rstrip('/')}/embeddings"

    expected_dim = None
    try:
        expected_dim = int(os.environ.get("LLM_EMBEDDING_DIM", "0")) or None
    except ValueError:
        pass

    vectors: list[list[float]] = []
    async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
        for start in range(0, len(texts), _embedding_batch_size()):
            batch = texts[start : start + _embedding_batch_size()]
            payload: dict[str, Any] = {"model": model, "input": batch}
            last_exc: Exception | None = None
            for attempt in range(2):
                try:
                    resp = await client.post(url, json=payload, headers=headers)
                    resp.raise_for_status()
                    data = resp.json().get("data")
                    if not isinstance(data, list):
                        raise EmbeddingUnavailable("embeddings response missing data")
                    items = sorted(data, key=lambda d: d.get("index", 0))
                    vectors.extend(item["embedding"] for item in items)
                    last_exc = None
                    break
                except Exception as exc:  # noqa: BLE001 — degrade via EmbeddingUnavailable
                    last_exc = exc
            if last_exc is not None:
                raise EmbeddingUnavailable(f"embedding request failed: {last_exc}") from last_exc

    if len(vectors) != len(texts):
        raise EmbeddingUnavailable(
            f"embedding count mismatch: expected {len(texts)}, got {len(vectors)}"
        )
    if expected_dim and vectors and len(vectors[0]) != expected_dim:
        logger.warning("embedding dim mismatch: expected %d, got %d", expected_dim, len(vectors[0]))
    return vectors


async def embed_query(text: str) -> list[float]:
    """Embed a single query string (convenience wrapper)."""
    return (await embed_texts([text]))[0]
