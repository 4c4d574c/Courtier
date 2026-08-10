"""ResultSummarizer — decide raw/summary/persist tiers and produce summaries."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from .result import ExecutionResult


class SummaryStrategy(Protocol):
    """Pluggable strategy for producing a summary and excerpts."""

    async def summarize(self, data: Any, actor_name: str) -> tuple[str, list[str]]:
        """Return (summary, key_excerpts)."""
        ...


class RuleBasedSummaryStrategy:
    """Simple rule-based summarizer used as the default."""

    async def summarize(self, data: Any, actor_name: str) -> tuple[str, list[str]]:
        if data is None:
            return f"{actor_name} 返回空结果", []

        if isinstance(data, str):
            return self._summarize_string(data, actor_name)

        if isinstance(data, (dict, list)):
            return self._summarize_json(data, actor_name)

        return f"{actor_name} 返回 {type(data).__name__}", [str(data)[:500]]

    def _summarize_string(self, text: str, actor_name: str) -> tuple[str, list[str]]:
        lines = [line for line in text.splitlines() if line.strip()]
        excerpts = lines[:5]
        if len(text) <= 200:
            summary = f"{actor_name}: {text}"
        else:
            summary = f"{actor_name} 返回文本 ({len(text)} 字符)，共 {len(lines)} 行"
        return summary, [ex[:500] for ex in excerpts]

    def _summarize_json(self, data: Any, actor_name: str) -> tuple[str, list[str]]:
        if isinstance(data, dict):
            keys = list(data.keys())
            summary = f"{actor_name} 返回对象，包含 {len(keys)} 个字段: {', '.join(keys[:10])}"
            excerpts = self._extract_nested_excerpts(data, max_excerpts=5, max_depth=8)
            if not excerpts:
                # Fallback to top-level keys when nothing meaningful is found
                excerpts = self._top_level_excerpts(data, keys)
            return summary, excerpts

        # list
        summary = f"{actor_name} 返回列表，共 {len(data)} 项"
        excerpts = []
        items = data[:5]
        for idx, item in enumerate(items):
            if isinstance(item, dict):
                nested = self._extract_nested_excerpts(item, max_excerpts=2, max_depth=7)
                if nested:
                    excerpts.extend(f"[{idx}]{e}" for e in nested)
                else:
                    ks = list(item.keys())[:5]
                    excerpts.append(f"[{idx}]: {', '.join(ks)}")
            else:
                excerpts.append(str(item)[:500])
        return summary, excerpts

    @staticmethod
    def _top_level_excerpts(data: dict, keys: list[str]) -> list[str]:
        """Fallback: simple top-level key-value excerpts."""
        excerpts: list[str] = []
        for key in keys[:5]:
            value = data[key]
            if isinstance(value, str):
                excerpts.append(f"{key}: {value[:500]}")
            elif isinstance(value, (dict, list)):
                excerpts.append(f"{key}: [{type(value).__name__}, len={len(value)}]")
            else:
                excerpts.append(f"{key}: {value}")
        return excerpts

    # Field names whose values are always internal identifiers (hashes, UUIDs,
    # internal refs).  These provide zero signal to the LLM so we skip them
    # when collecting excerpts.  Keep this list small — only truly opaque IDs.
    _METADATA_KEY_NAMES: frozenset[str] = frozenset(
        {
            "doc_id",
            "content_hash",
            "artifact_id",
            "ref_id",
            "session_id",
            "run_id",
            "trace_id",
        }
    )

    @classmethod
    def _extract_nested_excerpts(
        cls,
        obj: Any,
        max_excerpts: int = 5,
        max_depth: int = 8,
    ) -> list[str]:
        """Recursively sample meaningful leaf values from nested structures.

        Prioritises longer string values (likely content) over metadata and
        short values.  Hash-like strings (hex-only, >20 chars) and values
        from known metadata keys are deprioritised.  Returns field-path
        annotated excerpts like
        ``"pages[0].body.main_text[0].text: 为深入贯彻落实..."``.
        """
        candidates: list[tuple[str, str, bool]] = []  # (path, value, is_hash)
        cls._collect_string_leaves(obj, "", candidates, max_depth, max_excerpts * 3)

        if not candidates:
            return []

        # Sort by (non-hash, length) descending: content strings first,
        # hash-like / metadata strings last.  Long content strings are the
        # most useful to the LLM; a 64-char hex doc_id is meaningless.
        candidates.sort(key=lambda x: (not x[2], len(x[1])), reverse=True)
        return [f"{path}: {value[:500]}" for path, value, _ in candidates[:max_excerpts]]

    _HEX_RE = re.compile(r"^[0-9a-fA-F]{21,}$")

    @classmethod
    def _is_hash_like(cls, value: str, key_name: str) -> bool:
        """Return True when *value* looks like an opaque identifier string."""
        if key_name in cls._METADATA_KEY_NAMES:
            return True
        return bool(cls._HEX_RE.match(value))

    @classmethod
    def _collect_string_leaves(
        cls,
        obj: Any,
        path: str,
        out: list[tuple[str, str, bool]],
        max_depth: int,
        max_candidates: int,
    ) -> None:
        """Walk *obj* recursively, collecting (path, value, is_hash) for string leaves."""
        if len(out) >= max_candidates or max_depth <= 0:
            return

        if isinstance(obj, dict):
            for k, v in obj.items():
                child_path = f"{path}.{k}" if path else k
                cls._collect_string_leaves(v, child_path, out, max_depth - 1, max_candidates)

        elif isinstance(obj, list):
            # Sample up to 3 items from the list to keep cost bounded.
            for idx, item in enumerate(obj[:3]):
                child_path = f"{path}[{idx}]"
                cls._collect_string_leaves(item, child_path, out, max_depth - 1, max_candidates)

        elif isinstance(obj, str) and obj.strip():
            # Determine the leaf key name from the path for metadata detection.
            key_name = path.rsplit(".", 1)[-1] if "." in path else path
            is_hash = cls._is_hash_like(obj, key_name)
            out.append((path, obj, is_hash))
        # Non-string primitives (int, float, bool, None) are skipped.


@dataclass
class ResultSummarizer:
    """Decides how to present a result to the parent agent."""

    artifact_store: Any | None = None  # ArtifactStore for large-result persistence
    strategy: SummaryStrategy | None = None
    raw_inline_max_chars: int = 1_500
    # Deprecated: no longer consulted.  Persistence now triggers as soon as
    # the inline limit is exceeded so a dropped raw_data always has a $ref.
    summary_inline_max_chars: int = 6_000
    max_key_excerpts: int = 5
    excerpt_max_chars: int = 500

    async def from_data(
        self,
        *,
        success: bool,
        actor_type: Literal["tool", "agent", "skill"],
        actor_name: str,
        data: Any,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ExecutionResult:
        """Build an ExecutionResult from raw tool/agent data."""
        if not success:
            return ExecutionResult.from_error(
                actor_type=actor_type,
                actor_name=actor_name,
                error=error or "unknown error",
                metadata=metadata or {},
            )

        serialized = self._serialize(data)
        size_bytes = len(serialized.encode("utf-8"))
        content_type = "application/json" if isinstance(data, (dict, list)) else "text/plain"

        if len(serialized) <= self.raw_inline_max_chars:
            summary, _ = await self._strategy.summarize(data, actor_name)
            return ExecutionResult(
                success=True,
                actor_type=actor_type,
                actor_name=actor_name,
                raw_data=data,
                summary=summary,
                metadata=metadata or {},
                size_bytes=size_bytes,
                content_type=content_type,
            )

        summary, key_excerpts = await self._strategy.summarize(data, actor_name)
        excerpts = tuple(key_excerpts[: self.max_key_excerpts])

        # Above the inline limit (any actor type): persist to the artifact
        # store and return a reference with result_id.  Every summarised
        # result (raw_data dropped) must carry a recoverable $ref — this
        # closes the "death zone" (raw_inline_max_chars ~
        # summary_inline_max_chars) where tool results lost their raw_data
        # without gaining a result_id, leaving downstream consumers no way to
        # reload the full payload.  summary_inline_max_chars is retained for
        # constructor compatibility but no longer gates persistence.
        stored_ref_id: str | None = None
        stored_preview: str = ""
        stored_size: int = 0
        # Reuse the ref recorded by the registry-level persist (if any) so the
        # same payload is not written to disk twice under two different ref_ids.
        persisted_ref_id = (metadata or {}).get("persisted_ref_id")
        if persisted_ref_id:
            stored_ref_id = persisted_ref_id
            stored_preview = serialized[:200]
            stored_size = len(serialized)
        elif self._store is not None:
            persist_result = await self._store.persist(
                data,
                actor_name,
                force=True,
            )
            if persist_result.persisted:
                stored_ref_id = persist_result.ref_id
                stored_preview = (
                    persist_result.data.get("preview", "")[:200]
                    if isinstance(persist_result.data, dict)
                    else ""
                )
                stored_size = (
                    persist_result.data.get("size_chars", 0)
                    if isinstance(persist_result.data, dict)
                    else 0
                )

        return ExecutionResult(
            success=True,
            actor_type=actor_type,
            actor_name=actor_name,
            result_id=stored_ref_id,
            summary=summary,
            key_excerpts=excerpts,
            metadata={
                **(metadata or {}),
                "stored": (
                    {
                        "result_id": stored_ref_id,
                        "backend": "artifact_store",
                        "size_bytes": stored_size,
                        "preview": stored_preview,
                    }
                    if stored_ref_id
                    else None
                ),
            },
            size_bytes=size_bytes,
            content_type=content_type,
        )

    @property
    def _strategy(self) -> SummaryStrategy:
        return self.strategy or RuleBasedSummaryStrategy()

    @property
    def _store(self) -> Any | None:
        return self.artifact_store

    @staticmethod
    def _serialize(data: Any) -> str:
        if isinstance(data, str):
            return data
        try:
            return json.dumps(data, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(data)
