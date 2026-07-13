"""Plugin-side context manager and artifact store clients.

These clients forward to the host's CacheStore / ArtifactStore via JSON-RPC,
allowing plugin tools to use the same persistence and compaction
mechanisms as the in-process agent loop.
"""

from __future__ import annotations

from typing import Any

from .protocol import (
    METHOD_CACHE_PERSIST,
    METHOD_CACHE_RESOLVE,
    METHOD_CACHE_MICRO_COMPACT,
    METHOD_ARTIFACT_STORE_PUT,
    METHOD_ARTIFACT_STORE_GET,
    METHOD_ARTIFACT_STORE_LIST,
)


class PluginContextManager:
    """Lightweight context-manager replacement for plugin runtimes.

    Mirrors the public API of ``src.agent.core.context_manager.ContextManager``
    but delegates all storage operations to the host through a
    ``HostServiceClient``.
    """

    # Per-operation timeouts (seconds) — tuned to operation complexity:
    # persist may serialize large payloads, resolve/micro_compact are in-memory.
    _PERSIST_TIMEOUT = 60.0
    _RESOLVE_TIMEOUT = 10.0
    _MICRO_COMPACT_TIMEOUT = 120.0  # serializes and compacts full message history

    def __init__(self, host_client: Any, plugin_name: str = "plugin") -> None:
        self._client = host_client
        self._plugin_name = plugin_name

    def _prefixed_tool_name(self, tool_name: str) -> str:
        """Prefix tool names to avoid ref_id collisions with host tools."""
        return f"{self._plugin_name}.{tool_name}"

    async def persist_large_output(
        self, tool_name: str, data: Any, force: bool = False
    ) -> Any:
        """Persist large data on the host and return a marker dict (or original data)."""
        if data is None:
            return data
        result = await self._client.call(
            METHOD_CACHE_PERSIST,
            {
                "data": data,
                "tool_name": self._prefixed_tool_name(tool_name),
                "force": force,
            },
            timeout=self._PERSIST_TIMEOUT,
        )
        return result.get("data") if isinstance(result, dict) else data

    async def force_persist(self, tool_name: str, data: Any) -> str | None:
        """Persist data regardless of size and return its ref_id."""
        if data is None:
            return None
        result = await self._client.call(
            METHOD_CACHE_PERSIST,
            {
                "data": data,
                "tool_name": self._prefixed_tool_name(tool_name),
                "force": True,
            },
            timeout=self._PERSIST_TIMEOUT,
        )
        return result.get("ref_id") if isinstance(result, dict) else None

    async def resolve_refs(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        """Resolve ``$ref:...`` strings in tool arguments using the host cache."""
        return await self._client.call(
            METHOD_CACHE_RESOLVE, {"kwargs": kwargs},
            timeout=self._RESOLVE_TIMEOUT,
        )

    async def micro_compact(
        self, messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Replace old tool-result messages with omitted placeholders."""
        return await self._client.call(
            METHOD_CACHE_MICRO_COMPACT, {"messages": messages},
            timeout=self._MICRO_COMPACT_TIMEOUT,
        )

    async def compact_if_needed(
        self, messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """No-op in the first phase (Layer 3 LLM summarization is deferred)."""
        return messages


class HostArtifactStore:
    """Remote ArtifactStore client for plugins.

    Every method includes ``session_id`` so the host-side handler can locate
    the correct per-session ``ArtifactStore`` instance.
    """

    def __init__(self, host_client: Any, session_id: str = "") -> None:
        self._client = host_client
        self._session_id = session_id

    async def put(self, artifact: dict[str, Any]) -> dict[str, Any] | None:
        """Store an artifact in the host's per-session ArtifactStore."""
        return await self._client.call(
            METHOD_ARTIFACT_STORE_PUT,
            {"artifact": artifact, "session_id": self._session_id},
        )

    async def get(self, artifact_id: str) -> dict[str, Any] | None:
        """Retrieve an artifact by ID from the host."""
        return await self._client.call(
            METHOD_ARTIFACT_STORE_GET,
            {"artifact_id": artifact_id, "session_id": self._session_id},
        )

    async def list_all(self) -> list[dict[str, Any]]:
        """List all artifacts in the host's per-session ArtifactStore."""
        return await self._client.call(
            METHOD_ARTIFACT_STORE_LIST,
            {"session_id": self._session_id},
        )
