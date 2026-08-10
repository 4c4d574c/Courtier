"""Live visibility-scoped view over a shared ArtifactStore.

Sibling sub-agents dispatched by the same orchestrator share one root
``ArtifactStore``.  ``ScopedArtifactView`` wraps that store (instead of
snapshotting it) so a sub-agent keeps seeing artifacts its parent chain
creates — including ones added after spawn — while artifacts written by
sibling sub-agents stay invisible:

- **Writes** are stamped with the view's scope (the creator's
  ``AgentHandle.handle_id``) in ``metadata.subject`` before being delegated
  to the root store.  Artifacts written directly to the root store keep
  ``subject="unknown"`` and are treated as public.
- **Reads** are filtered: an artifact is visible when its ``subject`` is
  empty/``"unknown"``, equals the view's own scope, or is in the view's
  allowed set (inherited ancestor scopes plus an explicit allow-list such
  as ``AgentHandle.ref_ids``).  Hidden artifacts behave exactly as if they
  did not exist: listings omit them, ``read``/``get_info``/``load`` report
  "not found", and ``resolve_refs`` keeps the original ref string.

The view is intentionally **not** an ``ArtifactStore`` subclass: it shares
the persistence backend by delegation only, and ``isinstance(x,
ArtifactStore)`` checks keep treating it as a foreign object.  Snapshot /
restore for multi-turn sessions must go through the root store directly —
views exist only for the duration of a sub-agent run.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from ..core.cache_store import _EMBEDDED_REF_PATTERN, _REF_PATTERN
from .models import Artifact

logger = logging.getLogger(__name__)

# Subjects visible to every view: artifacts registered without a creator
# stamp (root-store writes, session restores, pre-isolation data).
_PUBLIC_SUBJECTS = frozenset({"", "unknown"})

# Placeholder ref used to mask hidden refs during resolve_refs delegation.
# It matches ``_REF_PATTERN`` but never resolves, so the backend keeps the
# masked string untouched and the view can swap the original ref back in.
_HIDDEN_SENTINEL_PREFIX = "$ref:__scoped_view_hidden__:"


class ScopedArtifactView:
    """A live, visibility-filtered view over a shared artifact store."""

    def __init__(
        self,
        store: Any,
        *,
        scope: str,
        allowed: Iterable[str] = (),
        extra_allowed: Iterable[str] = (),
    ) -> None:
        self._store = store
        self._scope = scope
        self._allowed = frozenset(allowed) | frozenset(extra_allowed)
        # Reads and writes delegate straight to the innermost non-view store.
        # Filtering happens once, here at the outermost view: the allowed-set
        # inheritance makes a child view's visibility a superset of every
        # ancestor view's, so composing through inner views would only
        # re-hide this view's own artifacts.
        root = store
        while isinstance(root, ScopedArtifactView):
            root = root._store
        self._root = root

    # -- Scope introspection (used by AgentRuntime for nested views) ---------

    @property
    def scope(self) -> str:
        """This view's own creator scope (a ``handle_id``)."""
        return self._scope

    @property
    def allowed(self) -> frozenset[str]:
        """Inherited/explicitly allowed creator scopes (own scope excluded)."""
        return self._allowed

    # -- Visibility rules ------------------------------------------------------

    def _subject_visible(self, subject: str | None) -> bool:
        if not subject or subject in _PUBLIC_SUBJECTS:
            return True
        return subject == self._scope or subject in self._allowed

    def _artifact_visible(self, artifact: Artifact) -> bool:
        return self._subject_visible(artifact.metadata.subject)

    def _ref_visible(self, ref_id: str) -> bool:
        # Refs without a registered typed artifact (e.g. summarizer-persisted
        # sub-agent results, skip-registration tools) were written outside
        # any view and are therefore public.
        artifact = self._root.get(ref_id)
        if artifact is None:
            return True
        return self._artifact_visible(artifact)

    # -- Write path: stamp creator scope, delegate to the root store ----------

    def _stamp(self, artifact: Artifact) -> Artifact:
        """Mark *artifact* as created by this view's scope."""
        if artifact.metadata.subject == self._scope:
            return artifact
        return artifact.model_copy(
            update={"metadata": artifact.metadata.model_copy(update={"subject": self._scope})}
        )

    def put(self, artifact: Artifact) -> Artifact:
        """Store an artifact, stamping it with this view's scope."""
        return self._root.put(self._stamp(artifact))

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
        """Register a cached ``$ref`` as a typed artifact owned by this scope.

        The caller-provided *subject* is always overridden: an artifact
        registered through a view belongs to the view's creator.
        """
        return self._root.register_cached_ref(
            ref_id=ref_id,
            artifact_type=artifact_type,
            created_by=created_by,
            data=data,
            role=role,
            subject=self._scope,
            projection_allowed=projection_allowed,
            debug_only=debug_only,
            persist_to_disk=persist_to_disk,
        )

    # -- Typed-artifact read path (filtered) ------------------------------------

    def get(self, artifact_id: str) -> Artifact | None:
        artifact = self._root.get(artifact_id)
        if artifact is None or not self._artifact_visible(artifact):
            return None
        return artifact

    def require(self, artifact_id: str) -> Artifact:
        artifact = self.get(artifact_id)
        if artifact is None:
            raise KeyError(f"Artifact not found: {artifact_id}")
        return artifact

    def list_all(self) -> list[Artifact]:
        return [a for a in self._root.list_all() if self._artifact_visible(a)]

    def list_visible(self) -> list[Artifact]:
        return [a for a in self._root.list_visible() if self._artifact_visible(a)]

    def list_projection_candidates(self) -> list[Artifact]:
        return [a for a in self._root.list_projection_candidates() if self._artifact_visible(a)]

    def find_by_type(self, artifact_type: str) -> list[Artifact]:
        return [a for a in self._root.find_by_type(artifact_type) if self._artifact_visible(a)]

    # -- Persistence read path (filtered) ---------------------------------------

    async def read(
        self,
        ref_id: str,
        *,
        query: str | None = None,
        chunk_index: int = 0,
        max_tokens: int = 2000,
    ) -> dict[str, Any]:
        if not self._ref_visible(ref_id):
            return {"error": f"result not found: {ref_id}", "data": None}
        return await self._root.read(
            ref_id,
            query=query,
            chunk_index=chunk_index,
            max_tokens=max_tokens,
        )

    def load(self, ref_id: str) -> Any:
        if not self._ref_visible(ref_id):
            return None
        return self._root.load(ref_id)

    def exists(self, ref_id: str) -> bool:
        if not self._ref_visible(ref_id):
            return False
        return self._root.exists(ref_id)

    def get_info(self, ref_id: str) -> dict[str, Any] | None:
        if not self._ref_visible(ref_id):
            return None
        return self._root.get_info(ref_id)

    @property
    def ref_map(self) -> dict[str, str]:
        return {k: v for k, v in self._root.ref_map.items() if self._ref_visible(k)}

    def resolve_refs(
        self,
        kwargs: dict[str, Any],
        param_schemas: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Resolve ``$ref`` strings, keeping hidden refs as literal strings.

        Hidden refs are masked with an unresolvable sentinel before
        delegation (the backend keeps unresolvable refs unchanged) and the
        original strings are restored afterwards — the same outcome the
        backend produces for refs that do not exist at all.
        """
        hidden: dict[str, str] = {}  # sentinel -> original ref
        reverse: dict[str, str] = {}  # original ref -> sentinel
        masked = self._mask_hidden_refs(kwargs, hidden, reverse)
        if not hidden:
            return self._root.resolve_refs(kwargs, param_schemas)
        resolved = self._root.resolve_refs(masked, param_schemas)
        return self._restore_hidden_refs(resolved, hidden)

    def _mask_hidden_refs(
        self,
        value: Any,
        hidden: dict[str, str],
        reverse: dict[str, str],
        depth: int = 0,
    ) -> Any:
        if depth > 32:
            return value
        if isinstance(value, str):
            if _REF_PATTERN.match(value):
                if self._ref_visible(value):
                    return value
                return self._sentinel_for(value, hidden, reverse)
            if _EMBEDDED_REF_PATTERN.search(value):
                return _EMBEDDED_REF_PATTERN.sub(
                    lambda m: (
                        m.group(0)
                        if self._ref_visible(m.group(0))
                        else self._sentinel_for(m.group(0), hidden, reverse)
                    ),
                    value,
                )
            return value
        if isinstance(value, dict):
            return {
                k: self._mask_hidden_refs(v, hidden, reverse, depth + 1) for k, v in value.items()
            }
        if isinstance(value, list):
            return [self._mask_hidden_refs(v, hidden, reverse, depth + 1) for v in value]
        return value

    @staticmethod
    def _sentinel_for(ref: str, hidden: dict[str, str], reverse: dict[str, str]) -> str:
        existing = reverse.get(ref)
        if existing is not None:
            return existing
        sentinel = f"{_HIDDEN_SENTINEL_PREFIX}{len(hidden) + 1}"
        hidden[sentinel] = ref
        reverse[ref] = sentinel
        return sentinel

    @staticmethod
    def _restore_hidden_refs(value: Any, hidden: dict[str, str], depth: int = 0) -> Any:
        if depth > 32:
            return value
        if isinstance(value, str):
            for sentinel, original in hidden.items():
                if sentinel in value:
                    value = value.replace(sentinel, original)
            return value
        if isinstance(value, dict):
            return {
                k: ScopedArtifactView._restore_hidden_refs(v, hidden, depth + 1)
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [ScopedArtifactView._restore_hidden_refs(v, hidden, depth + 1) for v in value]
        return value

    # -- Everything else delegates to the wrapped store -------------------------

    def __getattr__(self, name: str) -> Any:
        # Guard against protocol lookups (copy/pickle/ABC) and against
        # recursing on ``_store`` itself before __init__ assigned it.
        if name in ("_store", "_root") or (name.startswith("__") and name.endswith("__")):
            raise AttributeError(name)
        return getattr(self._store, name)
