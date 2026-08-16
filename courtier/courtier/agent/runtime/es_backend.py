"""ElasticsearchResultBackend — primary result persistence backend."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from courtier.config import get_settings
from courtier.es.client import get_es_client

logger = logging.getLogger(__name__)

_REF_ID_RE = re.compile(r"^\$ref:([a-zA-Z_][a-zA-Z0-9_.]*):(\d+)")

#: Module-level cache of per-tool max ref sequence, so per-request stores
#: don't re-aggregate ES on every agent build.  Values never decrease
#: within a process (a higher seen sequence wins), keeping numbering
#: monotonic even when the cache expires between two rapid builds.
_seq_seed_cache: dict[str, tuple[float, dict[str, int]]] = {}
_SEQ_SEED_TTL_SECONDS = 60.0


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
            "tool": {"type": "keyword"},
            "seq": {"type": "integer"},
        }
    }
}


class ElasticsearchResultBackend(ResultBackend):
    """ES-backed result store.

    The client is synchronous, so blocking calls are offloaded to a thread.
    """

    name = "elasticsearch"

    def __init__(self, index_name: str | None = None) -> None:
        # Default comes from the shared Settings singleton (same object the
        # host injects elsewhere) — a fresh Settings() would re-read env/.env
        # and could point reads/writes at a different index.
        self._index_name = index_name or get_settings().es_index_results
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
        ref_match = _REF_ID_RE.match(result_id)
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
        # tool/seq fields let new store instances seed their numbering past
        # anything already in the index — ref ids must not collide across
        # sessions/processes.
        if ref_match:
            body["tool"] = ref_match.group(1)
            body["seq"] = int(ref_match.group(2))

        await asyncio.to_thread(self._ensure_index)
        # op_type=create: a collision (two sessions issuing the same ref)
        # must fail loudly instead of overwriting another session's doc.
        await asyncio.to_thread(
            self._client.index,
            index=self._index_name,
            id=result_id,
            body=body,
            op_type="create",
        )
        # The TTL cache cannot see in-process writes; bump it so sibling
        # stores in this process continue past this sequence even inside
        # the TTL window.
        if ref_match and self._index_name in _seq_seed_cache:
            ts, seqs = _seq_seed_cache[self._index_name]
            tool = ref_match.group(1)
            if int(ref_match.group(2)) > seqs.get(tool, 0):
                seqs[tool] = int(ref_match.group(2))
                _seq_seed_cache[self._index_name] = (ts, seqs)

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

    def load(self, result_id: str) -> Any | None:
        """Synchronous direct read of the stored payload (``data`` field).

        Returns None when the document is missing or unreadable.  Used by
        ArtifactStore.load() as the fallback when disk has no record —
        refs persisted by another process/session live only in ES.
        """
        try:
            resp = self._client.get(index=self._index_name, id=result_id)
        except Exception:
            return None
        if not resp.get("found", True):
            return None
        return resp.get("_source", {}).get("data")

    def max_ref_sequences(self) -> dict[str, int]:
        """Max stored ref sequence per tool name (TTL-cached, monotonic).

        ArtifactStore seeds its numbering from this so two fresh stores
        (concurrent sessions, post-restart) never reissue the same
        ``$ref:<tool>:N`` and overwrite each other's index entries.
        """
        import time as _time

        cached = _seq_seed_cache.get(self._index_name)
        if cached and _time.monotonic() - cached[0] < _SEQ_SEED_TTL_SECONDS:
            return dict(cached[1])
        seqs: dict[str, int] = dict(cached[1]) if cached else {}
        try:
            # Aggregate over result_id only: it is an explicit keyword field
            # in every generation of this index, while tool/seq are dynamic
            # text in legacy indexes (fielddata-disabled → terms agg 400s).
            resp = self._client.search(
                index=self._index_name,
                body={
                    "size": 0,
                    "aggs": {
                        "by_result_id": {
                            "terms": {"field": "result_id", "size": 10000},
                        },
                    },
                },
            )
            for bucket in (
                resp.get("aggregations", {}).get("by_result_id", {}).get("buckets", [])
            ):
                match = _REF_ID_RE.match(str(bucket.get("key", "")))
                if match and int(match.group(2)) > seqs.get(match.group(1), 0):
                    seqs[match.group(1)] = int(match.group(2))
        except Exception:
            logger.warning("ref sequence seed query failed; numbering falls back", exc_info=True)
        _seq_seed_cache[self._index_name] = (_time.monotonic(), dict(seqs))
        return seqs
