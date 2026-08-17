"""ElasticsearchResultBackend — primary result persistence backend."""

from __future__ import annotations

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from courtier.config import get_settings
from courtier.es.client import get_es_client

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StoredResult:
    """Result of a store operation."""

    result_id: str
    backend: str
    size_bytes: int
    preview: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize for metadata."""
        return {
            "result_id": self.result_id,
            "backend": self.backend,
            "size_bytes": self.size_bytes,
            "preview": self.preview,
        }


class ResultBackend(ABC):
    """Abstract backend for persisting and retrieving large results."""

    name: str = "abstract"

    @abstractmethod
    async def store(
        self, result_id: str, data: Any, metadata: dict[str, Any] | None = None
    ) -> StoredResult:
        """Persist data and return a stored result descriptor."""
        ...

    @abstractmethod
    async def read(
        self,
        result_id: str,
        *,
        query: str | None = None,
        chunk_index: int = 0,
        max_tokens: int = 2000,
    ) -> dict[str, Any]:
        """Read a persisted result.

        Returns a dict with at least ``data`` (or ``error``) and ``metadata``.
        If ``query`` is provided, the backend may return matching excerpts.
        """
        ...

    @abstractmethod
    async def exists(self, result_id: str) -> bool:
        """Return True if the result exists in this backend."""
        ...

RESULT_INDEX_MAPPING = {
    "mappings": {
        "properties": {
            "result_id": {"type": "keyword"},
            "session_id": {"type": "keyword"},
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
    """ES-backed result store, isolated per session.

    Documents carry a ``session_id`` field and are keyed by the composite
    ``{session_id}#{result_id}`` — sessions never see or overwrite each
    other's results, and ``result_id`` values (``$ref:<tool>:N``) stay
    session-local (numbering restarts at 1 per session).

    The client is synchronous, so blocking calls are offloaded to a thread.
    """

    name = "elasticsearch"

    def __init__(self, index_name: str | None = None, session_id: str = "") -> None:
        # Default comes from the shared Settings singleton (same object the
        # host injects elsewhere) — a fresh Settings() would re-read env/.env
        # and could point reads/writes at a different index.
        self._index_name = index_name or get_settings().es_index_results
        self._session_id = session_id
        self._client = get_es_client()

    def _doc_id(self, result_id: str) -> str:
        """Composite document id; session-scoped when a session id is set."""
        return f"{self._session_id}#{result_id}" if self._session_id else result_id

    def _ensure_index(self) -> None:
        """Create the result index if it does not exist.

        On an existing index, backfill the ``session_id`` keyword mapping so
        new writes stay filterable without reindexing.
        """
        try:
            self._client.indices.create(index=self._index_name, body=RESULT_INDEX_MAPPING)
        except Exception as exc:
            if "resource_already_exists_exception" in str(exc):
                try:
                    self._client.indices.put_mapping(
                        index=self._index_name,
                        body={"properties": {"session_id": {"type": "keyword"}}},
                    )
                except Exception:
                    logger.warning("Could not update ES result index mapping", exc_info=True)
            else:
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
            "session_id": self._session_id,
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
        # op_type=create: a collision (the same session issuing a ref twice)
        # must fail loudly instead of overwriting the earlier document.
        await asyncio.to_thread(
            self._client.index,
            index=self._index_name,
            id=self._doc_id(result_id),
            body=body,
            op_type="create",
        )

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
            self._client.get, index=self._index_name, id=self._doc_id(result_id)
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
        must: list[dict[str, Any]] = [{"term": {"result_id": result_id}}]
        if self._session_id:
            must.append({"term": {"session_id": self._session_id}})
        body = {
            "query": {"bool": {"must": must + [{"match": {"data_text": query}}]}},
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
                self._client.exists, index=self._index_name, id=self._doc_id(result_id)
            )
            return bool(resp)
        except Exception as exc:
            logger.warning("ES exists check failed for %s: %s", result_id, exc)
            return False

    def load(self, result_id: str) -> Any | None:
        """Synchronous direct read of the stored payload (``data`` field).

        Returns None when the document is missing or unreadable.  Used by
        ArtifactStore.load() as the fallback when disk has no record.
        """
        try:
            resp = self._client.get(index=self._index_name, id=self._doc_id(result_id))
        except Exception:
            return None
        if not resp.get("found", True):
            return None
        return resp.get("_source", {}).get("data")
