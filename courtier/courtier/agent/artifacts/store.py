"""Typed artifact store with built-in disk persistence.

ArtifactStore is now the single persistence layer for the agent runtime.
It wraps an internal ``_PersistenceBackend`` (the former CacheStore) for
file I/O, ``$ref`` generation, and recursive ref resolution, while providing
typed artifact registration, projection, and scoped access.
"""

from __future__ import annotations

import logging
from typing import Any

from ..core.cache_store import _PersistenceBackend, PersistResult
from .models import (
    Artifact,
    ArtifactContext,
    ArtifactMetadata,
    get_artifact_schema,
    stable_content_hash,
)

logger = logging.getLogger(__name__)


def _log_persist_error(task: "asyncio.Task[Any]") -> None:
    """Log any unhandled exception from a fire-and-forget disk persist task."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("Disk persist task failed: %s", exc, exc_info=exc)


class ArtifactStore:
    """Typed artifact registry with built-in disk persistence for one agent run.

    Enforces artifact persistence and visibility policies declared in
    OutputArtifactContract (persist, llm_visible, projection_allowed, debug_only).

    Persistence, ``$ref`` resolution, and cached-result reads all go through
    this single store — there is no separate CacheStore to keep in sync.
    """

    def __init__(
        self,
        cache_dir: str = ".agent_cache",
        large_output_threshold: int = 3000,
        primary_backend: Any | None = None,
    ) -> None:
        self._artifacts: dict[str, Artifact] = {}
        # Per-type policy overrides registered from output contracts
        self._persist_policies: dict[str, str] = {}   # artifact_type -> always|auto|never
        self._visible_policies: dict[str, str] = {}   # artifact_type -> full|preview|summary|hidden
        # Internal persistence backend (the former CacheStore).
        # External callers use the delegating methods on this class instead
        # of reaching into ``_backend`` directly.
        self._backend = _PersistenceBackend(
            cache_dir=cache_dir,
            large_output_threshold=large_output_threshold,
            primary_backend=primary_backend,
        )

    # -- Persistence delegation (absorbed from CacheStore) ----------------------

    @property
    def cache_dir(self) -> str:
        """Return the disk cache directory used by the internal backend."""
        return str(self._backend._cache_dir)

    async def persist(
        self,
        data: Any,
        tool_name: str,
        *,
        force: bool = False,
        label: str | None = None,
        source_ref_id: str | None = None,
        source_query: str | None = None,
        tool_registry: Any = None,
    ) -> PersistResult:
        """Persist *data* to disk via the internal backend.

        Returns a ``PersistResult`` whose ``data`` attribute is either the
        original value (below threshold, not persisted) or a
        ``__persisted_output__`` marker dict (persisted).

        See ``_PersistenceBackend.persist`` for full semantics.
        """
        return await self._backend.persist(
            data,
            tool_name,
            force=force,
            label=label,
            source_ref_id=source_ref_id,
            source_query=source_query,
            tool_registry=tool_registry,
        )

    async def read(
        self,
        ref_id: str,
        *,
        query: str | None = None,
        chunk_index: int = 0,
        max_tokens: int = 2000,
    ) -> dict[str, Any]:
        """Read a persisted result by *ref_id*.

        Returns a dict with ``data`` (or ``error``) and ``metadata``.
        If *query* is provided, returns matching excerpts.
        """
        return await self._backend.read(
            ref_id,
            query=query,
            chunk_index=chunk_index,
            max_tokens=max_tokens,
        )

    def load(self, ref_id: str) -> Any:
        """Load persisted data for *ref_id* from disk (synchronous).

        Returns parsed JSON for ``.json`` files, raw text for ``.txt`` files,
        or ``None`` on failure.
        """
        return self._backend.load(ref_id)

    def set_ref(self, ref_id: str, filepath: str) -> None:
        """Register a ref_id → filepath mapping for multi-turn resolution.

        Safe for direct use by external components (e.g., artifact rehydration)
        that need to sync ref_map without going through :meth:`persist`.
        """
        self._backend.set_ref(ref_id, filepath)

    def exists(self, ref_id: str) -> bool:
        """Return ``True`` if *ref_id* has a persisted file on disk."""
        return self._backend.exists(ref_id)

    def get_info(self, ref_id: str) -> dict[str, Any] | None:
        """Return metadata for *ref_id*, or ``None`` if not found."""
        return self._backend.get_info(ref_id)

    def resolve_refs(
        self,
        kwargs: dict[str, Any],
        param_schemas: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Recursively resolve ``$ref`` strings in *kwargs*.

        When *param_schemas* is provided, adapts the loaded data type according
        to each parameter's schema ``type``.  Falls back gracefully: unknown
        refs and missing files keep the original ref string.
        """
        return self._backend.resolve_refs(kwargs, param_schemas)

    @property
    def ref_map(self) -> dict[str, str]:
        """Return the current ref_id → filepath mapping (read-only snapshot)."""
        return dict(self._backend.ref_map)

    # -- Type policies ----------------------------------------------------------

    def set_type_policy(
        self,
        artifact_type: str,
        *,
        persist: str = "auto",
        llm_visible: str = "summary",
    ) -> None:
        """Register persistence and visibility policies for an artifact type."""
        self._persist_policies[artifact_type] = persist
        self._visible_policies[artifact_type] = llm_visible

    # -- Artifact CRUD ----------------------------------------------------------

    def put(self, artifact: Artifact) -> Artifact:
        """Store an artifact, respecting its type policy.

        persist=never artifacts are silently dropped (not stored).
        """
        persist = self._persist_policies.get(artifact.artifact_type, "auto")
        if persist == "never":
            return artifact  # not stored
        self._artifacts[artifact.artifact_id] = artifact
        return artifact

    def get(self, artifact_id: str) -> Artifact | None:
        return self._artifacts.get(artifact_id)

    def require(self, artifact_id: str) -> Artifact:
        artifact = self.get(artifact_id)
        if artifact is None:
            raise KeyError(f"Artifact not found: {artifact_id}")
        return artifact

    def list_all(self) -> list[Artifact]:
        return list(self._artifacts.values())

    def list_visible(self) -> list[Artifact]:
        """List artifacts that should be visible to the LLM (not hidden)."""
        return [
            a
            for a in self._artifacts.values()
            if self._visible_policies.get(a.artifact_type, "summary") != "hidden"
        ]

    def list_projection_candidates(self) -> list[Artifact]:
        return [
            artifact
            for artifact in self._artifacts.values()
            if artifact.is_projection_candidate
        ]

    def find_by_type(self, artifact_type: str) -> list[Artifact]:
        return [
            artifact
            for artifact in self._artifacts.values()
            if artifact.artifact_type == artifact_type
        ]

    def register_cached_ref(
        self,
        *,
        ref_id: str,
        artifact_type: str,
        created_by: str,
        data: Any,
        role: str = "intermediate",
        subject: str = "unknown",
        projection_allowed: bool = True,
        debug_only: bool = False,
        persist_to_disk: bool = False,
    ) -> Artifact:
        """Register a cached ``$ref`` as a typed artifact.

        If *persist_to_disk* is ``True``, the data is also persisted to the
        internal backend so it survives across HTTP requests.  Defaults to
        ``False`` because the caller (ToolRegistry) already persists before
        registering.
        """
        metadata = ArtifactMetadata(
            created_by=created_by,
            content_hash=stable_content_hash(data),
            semantic_role=role,
            subject=subject,
            projection_allowed=projection_allowed,
            debug_only=debug_only,
        )
        schema = get_artifact_schema(artifact_type)
        artifact = Artifact(
            artifact_id=ref_id,
            artifact_type=artifact_type,
            schema_version=schema.schema_version if schema else "1.0",
            data=data,
            metadata=metadata,
        )
        # Optionally persist to disk so the data survives multi-turn sessions.
        if persist_to_disk and data is not None:
            # Fire-and-forget — best-effort disk write, errors are logged.
            import asyncio

            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None:
                task = loop.create_task(
                    self._backend.persist(data, created_by, force=True)
                )
                task.add_done_callback(_log_persist_error)
            else:
                logger.debug(
                    "No running event loop; skipping disk persist for %s", ref_id
                )
        return self.put(artifact)


class ScopedArtifactStore:
    """An ArtifactStore wrapper that enforces ArtifactContext permissions.

    Subagents receive a scoped store that filters which artifacts are
    visible and what operations (project, materialize, debug_read) are
    allowed on each one. Debug artifacts are excluded by default.
    """

    def __init__(self, store: ArtifactStore, context: ArtifactContext) -> None:
        self._store = store
        self._context = context
        # Build a fast lookup of allowed artifact ids and their permissions
        self._allowed: dict[str, bool] = {}
        self._can_project: set[str] = set()
        self._can_materialize: set[str] = set()
        self._can_debug: set[str] = set()
        for entry in context.allowed_artifacts:
            self._allowed[entry.ref] = True
            if entry.permissions.project:
                self._can_project.add(entry.ref)
            if entry.permissions.materialize:
                self._can_materialize.add(entry.ref)
            if entry.permissions.debug_read:
                self._can_debug.add(entry.ref)

    def _is_allowed(self, artifact_id: str) -> bool:
        return self._context.allow_all or artifact_id in self._allowed

    # -- Artifact methods -------------------------------------------------------

    def get(self, artifact_id: str) -> Artifact | None:
        if not self._is_allowed(artifact_id):
            return None
        return self._store.get(artifact_id)

    def require(self, artifact_id: str) -> Artifact:
        artifact = self.get(artifact_id)
        if artifact is None:
            raise KeyError(f"Artifact not found or not allowed: {artifact_id}")
        return artifact

    def list_projection_candidates(self) -> list[Artifact]:
        all_candidates = self._store.list_projection_candidates()
        if self._context.allow_all:
            return all_candidates
        return [a for a in all_candidates if a.artifact_id in self._can_project]

    def list_all(self) -> list[Artifact]:
        all_artifacts = self._store.list_all()
        if self._context.allow_all:
            return all_artifacts
        return [a for a in all_artifacts if self._is_allowed(a.artifact_id)]

    def put(self, artifact: Artifact) -> Artifact:
        """Store a new artifact (subagent can always produce new artifacts)."""
        return self._store.put(artifact)

    def register_cached_ref(
        self,
        *,
        ref_id: str,
        artifact_type: str,
        created_by: str,
        data: Any,
        role: str = "intermediate",
        subject: str = "unknown",
        projection_allowed: bool = True,
        debug_only: bool = False,
        persist_to_disk: bool = False,
    ) -> Artifact:
        return self._store.register_cached_ref(
            ref_id=ref_id,
            artifact_type=artifact_type,
            created_by=created_by,
            data=data,
            role=role,
            subject=subject,
            projection_allowed=projection_allowed,
            debug_only=debug_only,
            persist_to_disk=persist_to_disk,
        )

    # -- Persistence delegation (pass-through to inner store) -------------------

    @property
    def cache_dir(self) -> str:
        return self._store.cache_dir

    async def persist(
        self, data: Any, tool_name: str, *, force: bool = False,
        label: str | None = None, source_ref_id: str | None = None,
        source_query: str | None = None, tool_registry: Any = None,
    ) -> PersistResult:
        return await self._store.persist(
            data, tool_name, force=force, label=label,
            source_ref_id=source_ref_id, source_query=source_query,
            tool_registry=tool_registry,
        )

    async def read(
        self, ref_id: str, *, query: str | None = None,
        chunk_index: int = 0, max_tokens: int = 2000,
    ) -> dict[str, Any]:
        return await self._store.read(
            ref_id, query=query, chunk_index=chunk_index, max_tokens=max_tokens,
        )

    def load(self, ref_id: str) -> Any:
        return self._store.load(ref_id)

    def set_ref(self, ref_id: str, filepath: str) -> None:
        self._store.set_ref(ref_id, filepath)

    def exists(self, ref_id: str) -> bool:
        return self._store.exists(ref_id)

    def get_info(self, ref_id: str) -> dict[str, Any] | None:
        return self._store.get_info(ref_id)

    def resolve_refs(
        self, kwargs: dict[str, Any],
        param_schemas: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._store.resolve_refs(kwargs, param_schemas)

    @property
    def ref_map(self) -> dict[str, str]:
        return self._store.ref_map

    @property
    def can_materialize(self) -> set[str]:
        return self._can_materialize
