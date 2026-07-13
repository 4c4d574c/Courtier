"""ResultStore — pluggable storage for large execution results."""

from __future__ import annotations

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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


class DiskResultBackend(ResultBackend):
    """Disk-based fallback backend.

    Stores each result as a file under *cache_dir* keyed by *result_id*.
    """

    name = "disk"

    def __init__(self, cache_dir: str | Path = ".agent_cache") -> None:
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    def _result_path(self, result_id: str) -> Path:
        safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in result_id)
        return self._cache_dir / f"{safe_id}.json"

    def _serialize(self, data: Any) -> tuple[str, str]:
        if isinstance(data, str):
            return data, "text/plain"
        try:
            return json.dumps(data, ensure_ascii=False), "application/json"
        except (TypeError, ValueError):
            return str(data), "text/plain"

    async def store(
        self,
        result_id: str,
        data: Any,
        metadata: dict[str, Any] | None = None,
    ) -> StoredResult:
        serialized, content_type = self._serialize(data)
        path = self._result_path(result_id)
        await asyncio.to_thread(path.write_text, serialized, encoding="utf-8")
        return StoredResult(
            result_id=result_id,
            backend=self.name,
            size_bytes=len(serialized.encode("utf-8")),
            preview=serialized[:200],
        )

    async def read(
        self,
        result_id: str,
        *,
        query: str | None = None,
        chunk_index: int = 0,
        max_tokens: int = 2000,
    ) -> dict[str, Any]:
        path = self._result_path(result_id)
        if not path.exists():
            return {"error": f"result not found: {result_id}", "data": None}

        raw = await asyncio.to_thread(path.read_text, encoding="utf-8")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = raw

        if query:
            data = self._filter_by_query(self._as_text(data), query, max_tokens)
        else:
            data = self._truncate_data(data, max_tokens)

        return {
            "data": data,
            "metadata": {"backend": self.name, "result_id": result_id},
        }

    async def exists(self, result_id: str) -> bool:
        return self._result_path(result_id).exists()

    @staticmethod
    def _as_text(data: Any) -> str:
        if isinstance(data, str):
            return data
        try:
            return json.dumps(data, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(data)

    @staticmethod
    def _filter_by_query(text: str, query: str, max_tokens: int) -> str:
        """Naive substring filter; used as a fallback when ES is unavailable."""
        lines = text.splitlines()
        matches = [line for line in lines if query.lower() in line.lower()]
        if not matches:
            return text[: max_tokens * 4]
        joined = "\n".join(matches)
        return joined[: max_tokens * 4]

    @staticmethod
    def _truncate_data(data: Any, max_tokens: int) -> Any:
        """Truncate data to fit within *max_tokens* (roughly 4 chars/token)."""
        from ..core.loop_utils import truncate_data
        return truncate_data(data, max_tokens)


class ResultStore:
    """Multi-backend result store with Elasticsearch primary and disk fallback.

    .. deprecated::
        Use ``CacheStore`` from ``src.agent.core.cache_store`` instead.
        CacheStore now supports ``read()``, optional ES primary backend, and
        is the unified persistence layer for both tool outputs and sub-agent
        results.  ``ResultBackend`` / ``DiskResultBackend`` abstractions are
        kept for reuse by CacheStore's primary backend support.
    """

    def __init__(
        self,
        primary: ResultBackend | None = None,
        fallback: ResultBackend | None = None,
    ) -> None:
        self._primary = primary
        self._fallback = fallback or DiskResultBackend()
        self._active_backend: ResultBackend | None = None

    @property
    def active_backend(self) -> ResultBackend:
        """Return the currently chosen backend.

        If a primary backend is configured, it is probed once; on failure the
        disk fallback is used.
        """
        if self._active_backend is not None:
            return self._active_backend
        if self._primary is None:
            self._active_backend = self._fallback
            return self._active_backend
        self._active_backend = self._primary
        return self._active_backend

    async def store(
        self, result_id: str, data: Any, metadata: dict[str, Any] | None = None
    ) -> StoredResult:
        """Persist data using the active backend."""
        backend = self.active_backend
        try:
            return await backend.store(result_id, data, metadata)
        except Exception:
            logger.exception("Primary backend failed, trying fallback")
            if backend is self._fallback:
                raise
            return await self._fallback.store(result_id, data, metadata)

    async def read(
        self,
        result_id: str,
        *,
        query: str | None = None,
        chunk_index: int = 0,
        max_tokens: int = 2000,
    ) -> dict[str, Any]:
        """Read a result, falling back to disk if the primary fails."""
        backend = self.active_backend
        try:
            return await backend.read(
                result_id, query=query, chunk_index=chunk_index, max_tokens=max_tokens
            )
        except Exception:
            logger.exception("Primary backend read failed, trying fallback")
            if backend is self._fallback:
                raise
            return await self._fallback.read(
                result_id, query=query, chunk_index=chunk_index, max_tokens=max_tokens
            )

    async def exists(self, result_id: str) -> bool:
        backend = self.active_backend
        try:
            return await backend.exists(result_id)
        except Exception:
            if backend is self._fallback:
                raise
            return await self._fallback.exists(result_id)
