"""ElasticsearchResultBackend — primary result persistence backend."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from courtier.config import Settings
from courtier.es.client import get_es_client

from .store import ResultBackend, StoredResult

logger = logging.getLogger(__name__)

RESULT_INDEX_MAPPING = {
    "mappings": {
        "properties": {
            "result_id": {"type": "keyword"},
            "actor_type": {"type": "keyword"},
            "actor_name": {"type": "keyword"},
            "data": {"type": "object", "enabled": False},
            "data_text": {"type": "text"},
            "summary": {"type": "text"},
            "key_excerpts": {"type": "text"},
            "metadata": {"type": "object", "enabled": False},
            "size_bytes": {"type": "integer"},
            "content_type": {"type": "keyword"},
            "created_at": {"type": "date"},
        }
    }
}


class ElasticsearchResultBackend(ResultBackend):
    """ES-backed result store.

    The client is synchronous, so blocking calls are offloaded to a thread.
    """

    name = "elasticsearch"

    def __init__(self, index_name: str | None = None) -> None:
        self._index_name = index_name or Settings().es_index_results
        self._client = get_es_client()

    def _ensure_index(self) -> None:
        """Create the result index if it does not exist."""
        try:
            self._client.indices.create(index=self._index_name, body=RESULT_INDEX_MAPPING)
        except Exception as exc:
            if "resource_already_exists_exception" not in str(exc):
                logger.warning("Could not create ES result index: %s", exc)

    def _data_to_text(self, data: Any) -> str:
        if isinstance(data, str):
            return data
        try:
            return json.dumps(data, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(data)

    async def store(
        self, result_id: str, data: Any, metadata: dict[str, Any] | None = None
    ) -> StoredResult:
        meta = metadata or {}
        text = self._data_to_text(data)
        body = {
            "result_id": result_id,
            "actor_type": meta.get("actor_type", "unknown"),
            "actor_name": meta.get("actor_name", "unknown"),
            "data": data if isinstance(data, (dict, list)) else None,
            "data_text": text,
            "summary": meta.get("summary", ""),
            "key_excerpts": "\n".join(meta.get("key_excerpts", [])),
            "metadata": meta,
            "size_bytes": len(text.encode("utf-8")),
            "content_type": meta.get("content_type", "application/json"),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        await asyncio.to_thread(self._ensure_index)
        await asyncio.to_thread(self._client.index, index=self._index_name, id=result_id, body=body)

        return StoredResult(
            result_id=result_id,
            backend=self.name,
            size_bytes=body["size_bytes"],
            preview=text[:200],
        )

    async def read(
        self,
        result_id: str,
        *,
        query: str | None = None,
        chunk_index: int = 0,
        max_tokens: int = 2000,
    ) -> dict[str, Any]:
        if query:
            return await self._search(result_id, query, chunk_index, max_tokens)
        return await self._get(result_id, max_tokens)

    async def _get(self, result_id: str, max_tokens: int) -> dict[str, Any]:
        resp = await asyncio.to_thread(
            self._client.get, index=self._index_name, id=result_id
        )
        source = resp.get("_source", {})
        text = source.get("data_text", "")
        chars = max_tokens * 4
        return {
            "data": source.get("data") if source.get("data") is not None else text[:chars],
            "metadata": {
                "backend": self.name,
                "result_id": result_id,
                "actor_type": source.get("actor_type"),
                "actor_name": source.get("actor_name"),
                "size_bytes": source.get("size_bytes"),
            },
        }

    async def _search(
        self, result_id: str, query: str, chunk_index: int, max_tokens: int
    ) -> dict[str, Any]:
        from_ = chunk_index * 5
        size = 5
        body = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"result_id": result_id}},
                        {"match": {"data_text": query}},
                    ]
                }
            },
            "highlight": {
                "fields": {
                    "data_text": {"fragment_size": max_tokens * 4, "number_of_fragments": size}
                }
            },
            "from": from_,
            "size": size,
        }
        resp = await asyncio.to_thread(self._client.search, index=self._index_name, body=body)
        hits = resp.get("hits", {}).get("hits", [])
        fragments: list[str] = []
        for hit in hits:
            fragments.extend(hit.get("highlight", {}).get("data_text", []))
        if not fragments:
            # No highlights — fall back to getting the full doc and truncating.
            return await self._get(result_id, max_tokens)
        return {
            "data": "\n".join(fragments),
            "metadata": {
                "backend": self.name,
                "result_id": result_id,
                "query": query,
                "matches": len(hits),
            },
        }

    async def exists(self, result_id: str) -> bool:
        try:
            resp = await asyncio.to_thread(
                self._client.exists, index=self._index_name, id=result_id
            )
            return bool(resp)
        except Exception as exc:
            logger.warning("ES exists check failed for %s: %s", result_id, exc)
            return False
