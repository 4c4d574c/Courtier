"""Per-session ArtifactStore registry for host services exposed to plugins."""

from __future__ import annotations

import asyncio

from .store import ArtifactStore


class SessionArtifactStoreRegistry:
    """Keeps a mapping from session_id to the run's ArtifactStore.

    Plugin tools run in isolated subprocesses.  When a plugin uses the
    ``artifact_store`` host service, the host request handler looks up the
    store for the active session using this registry.
    """

    def __init__(self) -> None:
        self._stores: dict[str, ArtifactStore] = {}
        self._lock = asyncio.Lock()

    async def register(self, session_id: str, store: ArtifactStore) -> None:
        async with self._lock:
            self._stores[session_id] = store

    async def unregister(self, session_id: str) -> None:
        async with self._lock:
            self._stores.pop(session_id, None)

    def get(self, session_id: str) -> ArtifactStore | None:
        return self._stores.get(session_id)
