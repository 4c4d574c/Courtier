"""_PersistenceBackend — internal disk persistence for ArtifactStore.

This module is an implementation detail of ArtifactStore.  Do not import
_PersistenceBackend directly from outside ``src/agent/artifacts/`` —
use ArtifactStore's public methods instead.

First phase: JSON persist with ref_id generation, schema file creation, and
PersistResult.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..telemetry.metrics import record_context_ref_recover
from .schema_utils import extract_field_paths, merge_schema

logger = logging.getLogger(__name__)

# -- Constants ----------------------------------------------------------------

LARGE_OUTPUT_THRESHOLD = 3000
MAX_RECENT_FILES = 100
PREVIEW_MAX_CHARS = 200
_RESOLVE_MAX_DEPTH = 32
_HASH_INDEX_FILE = ".hash_index.json"

_REF_PATTERN = re.compile(r"^\$ref:([a-zA-Z_][a-zA-Z0-9_.]*):(\d+)(?::([a-zA-Z_][a-zA-Z0-9_]*))?$")

#: Process-wide floor for per-tool ref numbering.  Updated on every
#: persist; read by the ES backend's seed cache so a TTL-frozen snapshot
#: never hands a new store a number a sibling store just issued.
_SHARED_REF_COUNTERS: dict[str, int] = {}

# Pattern for finding $ref references embedded anywhere in a string
# (no ^/$ anchors).  Used as a fallback when a string value contains a
# ref but doesn't start with one.
_EMBEDDED_REF_PATTERN = re.compile(
    r"\$ref:([a-zA-Z_][a-zA-Z0-9_.]*):(\d+)(?::([a-zA-Z_][a-zA-Z0-9_]*))?"
)


def _default_cache_salt() -> str:
    """Default version salt mixed into the content-hash dedup key.

    Derived from the installed courtier distribution version so cache entries
    written by an older release are never reused after an upgrade (persisted
    result semantics may have changed).  Deployments that need explicit
    control can pass ``cache_salt=`` to ArtifactStore instead.
    """
    try:
        from importlib.metadata import PackageNotFoundError, version

        return version("courtier")
    except PackageNotFoundError:
        # Source checkout without an installed distribution — fall back to a
        # static salt.  Bump it manually when persisted semantics change.
        return "0"
    except Exception:  # pragma: no cover - defensive
        return "0"


# -- PersistResult ------------------------------------------------------------


@dataclass(frozen=True)
class PersistResult:
    """Outcome of a persist operation.

    Attributes:
        data: The original data (not persisted) or a ``__persisted_output__``
            marker dict (persisted).
        ref_id: The generated ref-id string (empty when not persisted).
        persisted: ``True`` when the data was written to disk.
    """

    data: Any
    ref_id: str
    persisted: bool


# -- _PersistenceBackend ------------------------------------------------------


class _PersistenceBackend:
    """Internal disk persistence backend for ArtifactStore.

    Thread-safe for concurrent access.  Provides file I/O, $ref generation,
    and recursive ref resolution.  Consumed only by ArtifactStore — external
    callers should use ``ArtifactStore.persist()``, ``ArtifactStore.resolve_refs()``,
    etc.

    Usage::

        backend = _PersistenceBackend(cache_dir=".agent_cache")
        result = await backend.persist(large_dict, "search_documents")
        if result.persisted:
            ref_id = result.data["ref_id"]   # e.g. "$ref:search_documents:1"
    """

    _REF_PATTERN = _REF_PATTERN
    _EMBEDDED_REF_PATTERN = _EMBEDDED_REF_PATTERN

    def __init__(
        self,
        cache_dir: str = ".agent_cache",
        large_output_threshold: int = LARGE_OUTPUT_THRESHOLD,
        preview_max_chars: int = PREVIEW_MAX_CHARS,
        primary_backend: Any | None = None,
        cache_salt: str | None = None,
    ) -> None:
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self.large_output_threshold = large_output_threshold
        self._preview_max_chars = preview_max_chars
        self._primary_backend = primary_backend
        # Version salt mixed into the dedup hash key: entries written before a
        # parser/plugin upgrade must not be reused, so the key is never a bare
        # content hash.  Defaults to the courtier distribution version.
        self._cache_salt = cache_salt if cache_salt is not None else _default_cache_salt()

        self._lock = asyncio.Lock()
        self.ref_map: dict[str, str] = {}
        self.ref_counters: dict[str, int] = {}
        self.recent_files: list[str] = []
        # Content-hash dedup index: "tool_name:sha256" -> {ref_id, file}.
        # Persisted in the cache dir so it survives per-request store
        # instances; stale entries (file deleted by GC) self-heal on lookup.
        self._hash_index: dict[str, dict[str, str]] = self._load_hash_index()

    # -- Content-hash dedup ------------------------------------------------------

    def _load_hash_index(self) -> dict[str, dict[str, str]]:
        index_path = self._cache_dir / _HASH_INDEX_FILE
        try:
            raw = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return {k: v for k, v in raw.items() if isinstance(v, dict)}

    def _save_hash_index(self) -> None:
        """Best-effort atomic write of the dedup index.

        Prunes entries whose target file no longer exists before writing so
        the index does not grow unboundedly (lookups self-heal individually,
        but without this the on-disk file only ever grew).
        """
        stale = [k for k, v in self._hash_index.items() if not Path(v.get("file", "")).exists()]
        for key in stale:
            self._hash_index.pop(key, None)
        index_path = self._cache_dir / _HASH_INDEX_FILE
        try:
            tmp_path = index_path.with_suffix(".tmp")
            tmp_path.write_text(json.dumps(self._hash_index, ensure_ascii=False), encoding="utf-8")
            tmp_path.replace(index_path)
        except OSError:
            logger.warning("Failed to persist hash index", exc_info=True)

    def _dedup_key(self, tool_name: str, serialized: str) -> str:
        """Version-salted dedup key: ``tool_name:salt:sha256``."""
        digest = hashlib.sha256(f"{self._cache_salt}\n{serialized}".encode("utf-8")).hexdigest()
        return f"{tool_name}:{digest}"

    def _dedup_lookup(self, tool_name: str, serialized: str) -> tuple[str, str] | None:
        """Return (ref_id, filepath) when identical content was persisted before.

        The dedup key is ``tool_name:salt:sha256`` — repeat calls of the SAME
        tool with identical output share one cache file, and entries written
        under a different version salt simply miss.  Entries whose file was
        removed (GC) are dropped and treated as a miss.
        """
        key = self._dedup_key(tool_name, serialized)
        entry = self._hash_index.get(key)
        if entry is None:
            return None
        filepath = entry.get("file", "")
        ref_id = entry.get("ref_id", "")
        if not ref_id or not filepath or not Path(filepath).exists():
            self._hash_index.pop(key, None)
            return None
        return ref_id, filepath

    def _dedup_record(self, tool_name: str, serialized: str, ref_id: str, filepath: str) -> None:
        self._hash_index[self._dedup_key(tool_name, serialized)] = {
            "ref_id": ref_id,
            "file": filepath,
        }
        self._save_hash_index()

    # -- Public API -----------------------------------------------------------

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
        """Persist *data* to disk when it is large (or when *force* is set).

        Returns a ``PersistResult`` whose ``data`` attribute is either the
        original value (below threshold, not persisted) or a
        ``__persisted_output__`` marker dict (persisted).
        """
        if data is None:
            return PersistResult(data=None, ref_id="", persisted=False)

        content_type, serialized = self._detect_content_type(data)

        if not force and len(serialized) <= self.large_output_threshold:
            return PersistResult(data=data, ref_id="", persisted=False)

        # Content-hash dedup: identical output from the same tool reuses the
        # existing cache file instead of writing a duplicate.
        dedup_hit = self._dedup_lookup(tool_name, serialized)
        if dedup_hit is not None:
            ref_id, filepath_str = dedup_hit
            async with self._lock:
                self.ref_map[ref_id] = filepath_str
            marker = self._build_marker(
                ref_id,
                filepath_str,
                serialized,
                content_type,
                label=label,
                source_ref_id=source_ref_id,
                source_query=source_query,
            )
            marker["data_shape"] = self._build_data_shape(data)
            marker["dedup_hit"] = True
            logger.debug("persist dedup hit: %s -> %s", tool_name, ref_id)
            return PersistResult(data=marker, ref_id=ref_id, persisted=True)

        ref_id = self._next_ref_id(tool_name, label)
        ext = "txt" if content_type == "text/plain" else "json"
        filepath_str = await self._write_file(tool_name, ref_id, serialized, ext)
        self._dedup_record(tool_name, serialized, ref_id, filepath_str)

        # Persist schema alongside data (best-effort)
        if content_type == "application/json" and isinstance(data, (dict, list)):
            self._persist_schema(data, filepath_str, tool_name, tool_registry)

        # Build the __persisted_output__ marker
        marker = self._build_marker(
            ref_id,
            filepath_str,
            serialized,
            content_type,
            label=label,
            source_ref_id=source_ref_id,
            source_query=source_query,
        )
        marker["data_shape"] = self._build_data_shape(data)

        # Optionally persist to primary backend (e.g. ES) — best-effort.
        if self._primary_backend is not None:
            try:
                await self._primary_backend.store(
                    ref_id,
                    data,
                    metadata={"tool_name": tool_name, "label": label},
                )
            except Exception:
                logger.warning(
                    "Primary backend persist failed for %s (data still on disk)",
                    ref_id,
                )

        return PersistResult(data=marker, ref_id=ref_id, persisted=True)

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
        # Try primary backend first (e.g. ES), fall back to disk.
        data: Any = None
        metadata: dict[str, Any] = {"backend": "disk", "ref_id": ref_id}

        if self._primary_backend is not None:
            try:
                primary_result: dict[str, Any] = await self._primary_backend.read(
                    ref_id, query=query, chunk_index=chunk_index, max_tokens=max_tokens
                )
                if "error" not in primary_result:
                    return primary_result
                metadata["primary_error"] = primary_result.get("error")
            except Exception as exc:
                logger.warning(
                    "Primary backend read failed for %s, falling back to disk",
                    ref_id,
                )
                metadata["primary_error"] = f"primary_backend_failed: {exc}"

        # Disk fallback
        data = self.load(ref_id)
        if data is None:
            return {"error": f"result not found: {ref_id}", "data": None}

        if query:
            data = self._filter_by_query(self._as_text(data), query, max_tokens)
        else:
            data = self._truncate_data(data, max_tokens)

        return {"data": data, "metadata": metadata}

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
        from .loop_utils import truncate_data

        return truncate_data(data, max_tokens)

    # -- Helper methods -------------------------------------------------------

    @staticmethod
    def _sanitize_label(label: str) -> str:
        """Sanitize *label* to match ``_REF_PATTERN`` group ``[a-zA-Z_][a-zA-Z0-9_]*``.

        Returns the sanitized label.  If the label was changed, a warning is
        logged so callers are aware of the transformation.
        """
        original = label
        # Replace any character that is not alphanumeric or underscore.
        sanitized = re.sub(r"[^a-zA-Z0-9_]", "_", label)
        # Must start with a letter or underscore, never a digit.
        if sanitized and sanitized[0].isdigit():
            sanitized = "_" + sanitized
        if sanitized != original:
            logger.warning(
                "Label %r sanitized to %r to match ref_id pattern.",
                original,
                sanitized,
            )
        return sanitized

    def seed_ref_counters(self, mapping: dict[str, int]) -> None:
        """Raise per-tool numbering past externally known maxima.

        Never lowers existing counters: in-memory numbering from earlier
        persists (or a larger later max) always wins, so concurrent stores
        seeded from the same snapshot still diverge upward.  Used with the
        ES result index so separate processes/sessions never reissue the
        same ``$ref:<tool>:N`` (its _id would overwrite a prior session's
        document).
        """
        for tool, seq in mapping.items():
            if seq > self.ref_counters.get(tool, 0):
                self.ref_counters[tool] = seq

    def _next_ref_id(self, tool_name: str, label: str | None) -> str:
        """Return the next ref-id for *tool_name* (not async-safe — callers
        must hold ``self._lock`` or be single-threaded)."""
        floor = _SHARED_REF_COUNTERS.get(tool_name, 0)
        if floor > self.ref_counters.get(tool_name, 0):
            self.ref_counters[tool_name] = floor
        seq = self.ref_counters.get(tool_name, 0) + 1
        self.ref_counters[tool_name] = seq
        if seq > _SHARED_REF_COUNTERS.get(tool_name, 0):
            _SHARED_REF_COUNTERS[tool_name] = seq

        ref_id = f"$ref:{tool_name}:{seq}"
        if label:
            sanitized = self._sanitize_label(label)
            if sanitized:
                ref_id += f":{sanitized}"
        return ref_id

    async def _write_file(self, tool_name: str, ref_id: str, serialized: str, ext: str) -> str:
        """Write *serialized* to disk and update ``ref_map`` / ``recent_files``.

        Returns the absolute or relative path string of the written file.
        File I/O is offloaded to a thread-pool executor to avoid blocking
        the event loop.
        """
        match = self._REF_PATTERN.match(ref_id)
        seq = match.group(2) if match else "0"
        ts = int(time.time() * 1000)
        # Uniqueness suffix: ref_counters are per-instance, so two fresh stores
        # (concurrent requests, or a post-upgrade store) can emit the same seq
        # within the same millisecond — without entropy they would overwrite
        # each other's cache file.
        uniq = uuid4().hex[:6]
        safe_name = tool_name.replace("/", "_").replace(" ", "_")
        filename = f"{safe_name}_{seq}_{ts}_{uniq}.{ext}"
        filepath = self._cache_dir / filename
        await asyncio.to_thread(filepath.write_text, serialized, encoding="utf-8")
        filepath_str = str(filepath)

        async with self._lock:
            self.ref_map[ref_id] = filepath_str
            self.recent_files.append(filepath_str)
            if len(self.recent_files) > MAX_RECENT_FILES:
                self.recent_files = self.recent_files[-MAX_RECENT_FILES:]

        return filepath_str

    def _persist_schema(
        self,
        data: Any,
        filepath_str: str,
        tool_name: str,
        tool_registry: Any,
    ) -> None:
        """Extract field paths, merge with base schema, and write ``.schema.json``.

        Best-effort: exceptions are silently caught.
        """
        try:
            actual_paths = extract_field_paths(data)
            base_schema = None
            if tool_registry is not None:
                base_schema = tool_registry.get_output_schema(tool_name)
            merged = merge_schema(base_schema, actual_paths)

            if filepath_str.endswith(".json"):
                schema_path_str = filepath_str[:-5] + ".schema.json"
            else:
                schema_path_str = filepath_str + ".schema.json"
            Path(schema_path_str).write_text(
                json.dumps(merged, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            logger.warning("Schema extraction failed for %s", filepath_str, exc_info=True)

    @staticmethod
    def _build_data_shape(data: Any) -> dict[str, Any]:
        """Build a lightweight structural summary of *data*.

        Returns a dict with ``type`` and type-specific hints so the LLM can
        understand the data shape without any tool call.
        """
        if isinstance(data, dict):
            shape: dict[str, Any] = {"type": "object", "keys": list(data.keys())}
            for k, v in data.items():
                if isinstance(v, list):
                    shape[f"_{k}_len"] = len(v)
            # Include representative field paths for nested structures
            # so the LLM can inspect data shape without extra calls
            field_paths = _sample_field_paths(data, max_depth=7, max_paths=15)
            if field_paths:
                shape["_field_paths"] = field_paths
            return shape
        if isinstance(data, list):
            shape = {"type": "array", "len": len(data)}
            if data and isinstance(data[0], dict):
                shape["item_keys"] = list(data[0].keys())
                field_paths = _sample_field_paths(data[0], max_depth=7, max_paths=15)
                if field_paths:
                    shape["_item_field_paths"] = [f"[0]{p}" for p in field_paths]
            return shape
        if isinstance(data, str):
            return {"type": "string", "len": len(data)}
        return {"type": type(data).__name__}

    def _build_marker(
        self,
        ref_id: str,
        filepath_str: str,
        serialized: str,
        content_type: str,
        *,
        label: str | None,
        source_ref_id: str | None,
        source_query: str | None,
    ) -> dict[str, Any]:
        """Build the ``__persisted_output__`` marker dict for *persist*."""
        preview_max = self._preview_max_chars
        preview = serialized[:preview_max]
        if len(serialized) > preview_max:
            preview += (
                f"\n...[truncated, full output ({len(serialized)} chars)"
                f" saved to {filepath_str}]"
            )

        marker: dict[str, Any] = {
            "__persisted_output__": True,
            "ref_id": ref_id,
            "file": filepath_str,
            "size_chars": len(serialized),
            "preview": preview,
            "content_type": content_type,
        }

        if label is not None:
            marker["label"] = label

        if source_ref_id is not None or source_query is not None:
            source: dict[str, str] = {}
            if source_ref_id is not None:
                source["ref_id"] = source_ref_id
            if source_query is not None:
                source["query"] = source_query
            marker["source"] = source

        return marker

    def _detect_content_type(self, data: Any) -> tuple[str, str]:
        """Return ``(content_type, serialized)`` for *data*.

        * ``dict`` / ``list``  -> ``"application/json"``, JSON-serialised string
        * ``str`` that is valid JSON -> ``"application/json"``, original string
        * ``str`` that is not valid JSON -> ``"text/plain"``, original string
        """
        if isinstance(data, (dict, list)):
            return "application/json", json.dumps(data, ensure_ascii=False)

        if isinstance(data, str):
            try:
                json.loads(data)
                return "application/json", data
            except (json.JSONDecodeError, ValueError):
                return "text/plain", data

        # Fallback for other types
        return "text/plain", str(data)

    # -- Ref resolution ------------------------------------------------------

    def _load_ref(self, ref_id: str) -> tuple[Any, str] | tuple[None, None]:
        """Load data from disk for *ref_id*.

        Returns ``(data, content_type)`` or ``(None, None)`` on failure.
        """
        filepath = self.ref_map.get(ref_id)
        if filepath is None:
            return None, None

        path = Path(filepath)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None, None

        if path.suffix == ".json":
            try:
                return json.loads(raw), "application/json"
            except json.JSONDecodeError:
                return raw, "text/plain"
        else:
            return raw, "text/plain"

    def load(self, ref_id: str) -> Any:
        """Load persisted data for *ref_id*.

        Disk first (parsed JSON for ``.json`` files, raw text for ``.txt``);
        on a miss the primary backend (e.g. the ES result index) is asked
        synchronously, so refs persisted by another process/session remain
        loadable.  ``None`` on failure.
        """
        data, _ = self._load_ref(ref_id)
        if data is not None:
            return data
        backend = self._primary_backend
        loader = getattr(backend, "load", None)
        if callable(loader):
            try:
                return loader(ref_id)
            except Exception:
                logger.warning("primary backend load failed for %s", ref_id, exc_info=True)
        return None

    def set_ref(self, ref_id: str, filepath: str) -> None:
        """Register a ref_id → filepath mapping for multi-turn resolution.

        Safe for direct use by external components (e.g., artifact rehydration)
        that need to sync ref_map without going through :meth:`store`.

        Also advances the per-tool numbering counter past any numeric ref it
        sees: session restore re-registers historical refs via this method,
        and without the counter bump a later persist would reissue
        ``$ref:<tool>:1`` and silently overwrite the restored entry.
        """
        self.ref_map[ref_id] = filepath
        match = self._REF_PATTERN.match(ref_id)
        if match:
            prefix, seq = match.group(1), int(match.group(2))
            if seq > self.ref_counters.get(prefix, 0):
                self.ref_counters[prefix] = seq

    def exists(self, ref_id: str) -> bool:
        """Return ``True`` if *ref_id* has a persisted file on disk."""
        filepath = self.ref_map.get(ref_id)
        if filepath is None:
            return False
        return Path(filepath).exists()

    def get_info(self, ref_id: str) -> dict[str, Any] | None:
        """Return metadata for *ref_id*, or ``None`` if not found."""
        filepath = self.ref_map.get(ref_id)
        if filepath is None:
            return None

        path = Path(filepath)
        if not path.exists():
            return None

        stat = path.stat()
        return {
            "ref_id": ref_id,
            "file": filepath,
            "size_bytes": stat.st_size,
            "exists": True,
        }

    #: Fields probed, in order, when adapting a persisted dict to a string
#: parameter — the model should be able to pass ``$ref:convert_document:N``
#: straight into a text parameter and receive the document text, not the
#: serialized ``{"markdown": ..., "format": ...}`` envelope.
    def _load_and_adapt(self, ref_id: str, param_schema: dict[str, Any] | None = None) -> Any:
        """Load data for *ref_id* and adapt based on *param_schema* type.

        Returns ``None`` when the ref cannot be loaded, signalling that the
        original ref string should be kept as-is.
        """
        data, _content_type = self._load_ref(ref_id)
        if data is None:
            return None  # Signal failure to caller

        if param_schema is None:
            return data

        schema_type = param_schema.get("type", "")

        if schema_type == "string":
            if isinstance(data, str):
                return data
            if isinstance(data, dict):
                # Text params expect prose, not a JSON envelope: pull the
                # document text out of wrapper objects.
                for field_name in _TEXT_FIELD_PRIORITY:
                    value = data.get(field_name)
                    if isinstance(value, str) and value.strip():
                        return value
            return json.dumps(data, ensure_ascii=False)

        elif schema_type == "object":
            if isinstance(data, dict):
                return data
            if isinstance(data, str):
                try:
                    parsed = json.loads(data)
                    if isinstance(parsed, dict):
                        return parsed
                except (json.JSONDecodeError, ValueError):
                    pass
                return data  # Fallback: non-JSON text returned as-is
            return None  # Cannot adapt to object — signal failure

        elif schema_type == "array":
            if isinstance(data, list):
                return data
            if isinstance(data, str):
                try:
                    parsed = json.loads(data)
                    if isinstance(parsed, list):
                        return parsed
                except (json.JSONDecodeError, ValueError):
                    pass
                return data  # Fallback: non-JSON text returned as-is
            return None  # Cannot adapt to array — signal failure

        elif schema_type == "number":
            try:
                return float(data)
            except (ValueError, TypeError):
                return data

        elif schema_type == "integer":
            try:
                return int(data)
            except (ValueError, TypeError):
                return data

        elif schema_type == "boolean":
            if isinstance(data, bool):
                return data
            if isinstance(data, str):
                return data.lower() in ("true", "1", "yes")
            return bool(data)

        return data

    def _resolve_value(
        self,
        value: Any,
        schema: dict[str, Any] | None = None,
        depth: int = 0,
    ) -> Any:
        """Recursively resolve ``$ref`` strings in *value*.

        When *schema* is provided, adapts the loaded data type according
        to the schema ``type``.  Falls back gracefully: unknown refs and
        missing files keep the original ref string.  Maximum recursion
        depth is 32 to prevent infinite loops.
        """
        if depth > _RESOLVE_MAX_DEPTH:
            return value

        if isinstance(value, str):
            m = self._REF_PATTERN.match(value)
            if m:
                result = self._load_and_adapt(value, schema)
                if result is not None:
                    record_context_ref_recover("hit")
                    return self._resolve_value(result, schema, depth + 1)
                record_context_ref_recover("miss")
                return value  # Keep original on failure

            # Fallback: resolve $ref patterns embedded inside a longer string.
            # This handles cases where the LLM writes a $ref inside a prose
            # task description (e.g. "审计文档：$ref:parse_document:1") instead
            # of passing it as a standalone parameter value.
            if self._EMBEDDED_REF_PATTERN.search(value):
                resolved_val = self._resolve_embedded_refs(value, depth)
                if resolved_val is not None:
                    return resolved_val
            return value

        if isinstance(value, dict):
            # Check for __persisted_output__ marker at this level
            if value.get("__persisted_output__"):
                ref_id = value.get("ref_id")
                if ref_id:
                    loaded = self._load_and_adapt(ref_id, schema)
                    if loaded is not None:
                        record_context_ref_recover("hit")
                        return self._resolve_value(loaded, schema, depth + 1)
                    record_context_ref_recover("miss")
                    return ref_id  # Keep ref_id string on failure

            resolved_dict: dict[str, Any] = {}
            for k, v in value.items():
                resolved_dict[k] = self._resolve_value(v, schema, depth + 1)
            return resolved_dict

        if isinstance(value, list):
            return [self._resolve_value(item, schema, depth + 1) for item in value]

        return value

    def _resolve_embedded_refs(self, value: str, depth: int) -> Any:
        """Resolve ``$ref`` patterns embedded inside a longer string.

        Strategy:
        1. Find all $ref occurrences in the string.
        2. Resolve each ref from cache — if they all resolve to strings,
           perform string substitution.
        3. If the single ref resolves to a dict/list, return the resolved
           data directly (replacing the entire string value).
        4. If multiple refs and some resolve to complex types, return the
           original string unchanged — the caller already gets a warning log.
        """
        # Find all embedded refs
        refs: list[str] = []
        for m in self._EMBEDDED_REF_PATTERN.finditer(value):
            refs.append(m.group(0))

        if not refs:
            return value

        # Resolve each ref
        substitutions: dict[str, Any] = {}
        all_strings = True
        for full_match in refs:
            data = self._load_and_adapt(full_match, None)
            if data is None or data == full_match:
                # Ref could not be resolved — keep original
                substitutions[full_match] = full_match
            elif not isinstance(data, str):
                all_strings = False
                substitutions[full_match] = data
            else:
                substitutions[full_match] = data

        if not all_strings:
            # If there's exactly one ref and it resolved to a complex type,
            # replace the entire value with the resolved data.
            if len(refs) == 1:
                result = substitutions[refs[0]]
                if not isinstance(result, str):
                    return self._resolve_value(result, None, depth + 1)
            # Multiple refs with mixed types — can't merge into a string.
            # Return original value so callers can inspect it.
            logger.warning(
                "Cannot resolve embedded refs with complex types in: %s",
                value[:200],
            )
            return value

        # All resolved data are strings — perform substitution
        result = value
        for full_match, replacement in substitutions.items():
            result = result.replace(full_match, replacement)
        return result

    def resolve_refs(
        self,
        kwargs: dict[str, Any],
        param_schemas: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Recursively resolve ``$ref`` strings in *kwargs*.

        When *param_schemas* is provided, adapts the loaded data type according
        to each parameter's schema ``type``.  Falls back gracefully: unknown
        refs and missing files keep the original ref string.  Maximum recursion
        depth is 32 to prevent infinite loops.
        """
        resolved_kwargs: dict[str, Any] = {}
        for key, value in kwargs.items():
            key_schema = param_schemas.get(key) if param_schemas else None
            resolved_kwargs[key] = self._resolve_value(value, key_schema)

        return resolved_kwargs


#: Fields probed, in order, when adapting a persisted dict to a string
#: parameter — the model should be able to pass ``$ref:convert_document:N``
#: straight into a text parameter and receive the document text, not the
#: serialized ``{"markdown": ..., "format": ...}`` envelope.
_TEXT_FIELD_PRIORITY = ("markdown", "text", "content", "plain_text", "chunk_text", "data")


def _sample_field_paths(data: Any, *, max_depth: int = 3, max_paths: int = 12) -> list[str]:
    """Extract representative field-path strings from nested data.

    Prioritizes leaf paths (scalar values) over container paths,
    capped at *max_paths*.  Non-destructive — just a sampler for the LLM.
    """
    paths: list[str] = []
    _collect_paths(data, "", paths, max_depth, max_paths)
    return paths[:max_paths]


def _collect_paths(obj: Any, prefix: str, out: list[str], max_depth: int, max_paths: int) -> None:
    if len(out) >= max_paths:
        return
    if max_depth <= 0:
        return

    if isinstance(obj, dict):
        for k, v in obj.items():
            path = f".{k}" if not prefix else f"{prefix}.{k}"
            if v is None or isinstance(v, (str, int, float, bool)):
                out.append(path)
            elif isinstance(v, list):
                if v and isinstance(v[0], dict):
                    _collect_paths(v[0], f"{path}[0]", out, max_depth - 1, max_paths)
                elif v:
                    _collect_paths(v[0], f"{path}[]", out, max_depth - 1, max_paths)
                else:
                    out.append(f"{path}[]")
            elif isinstance(v, dict):
                _collect_paths(v, path, out, max_depth - 1, max_paths)
    elif isinstance(obj, list):
        if obj and isinstance(obj[0], dict):
            _collect_paths(obj[0], f"{prefix}[0]", out, max_depth - 1, max_paths)


# Backward-compatibility alias.  New code should use ArtifactStore instead;
# this alias exists so existing imports continue to work during the migration.
CacheStore = _PersistenceBackend
