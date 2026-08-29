"""MemoryManager — four-tier memory for the agent runtime.

Tiers:
  1. working: the messages currently in the LLM context window.
  2. session: per-run facts that survive individual turns but are scoped to
     the current session/task.
  3. long_term: persistent knowledge across sessions (user preferences,
     learned patterns, entity summaries).
  4. retrieval: searchable memory used to recall relevant long-term/session
     facts on demand (naive keyword / embedding placeholder).

The manager keeps ``ContextManager``'s three-layer context-budget behaviour
(compaction, large-output persistence, ref resolution) as its working-memory
engine. It adds explicit session/long_term/retrieval APIs that previously were
handled ad-hoc or not at all.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from itertools import groupby
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from courtier.agent.memory.store import FileMemoryStore, MemoryStore

from .context_manager import (
    COMPACT_TARGET_TOKENS,
    LARGE_OUTPUT_THRESHOLD,
    MAX_CONTEXT_TOKENS,
    MICRO_COMPACT_TOKENS,
    RECENT_TOOL_RESULTS_TOKENS,
    ContextManager,
    _build_summary,
    _is_cjk,
)
from .state import Message

if TYPE_CHECKING:
    from .model import ModelClient

logger = logging.getLogger(__name__)

MemoryTier = Literal["working", "session", "long_term", "retrieval"]


@dataclass
class MemoryQuery:
    """A query against retrieval memory."""

    text: str
    top_k: int = 5
    tier: MemoryTier | None = None
    filters: dict[str, Any] = field(default_factory=dict)


@dataclass
class MemoryRecall:
    """A single retrieved memory item."""

    key: str
    value: Any
    tier: MemoryTier
    score: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)


class MemoryManager(ContextManager):
    """Context manager extended with explicit session/long-term/retrieval tiers.

    Backwards compatibility: all ``ContextManager`` methods are preserved and
    behave identically. New code can use the async memory APIs while old code
    continues to call ``micro_compact`` / ``compact_if_needed``.
    """

    def __init__(
        self,
        model: "ModelClient" | None = None,
        cache_dir: str = ".agent_cache",
        memory_store: MemoryStore | None = None,
        session_id: str = "default",
        max_context_tokens: int = MAX_CONTEXT_TOKENS,
        micro_compact_tokens: int = MICRO_COMPACT_TOKENS,
        compact_target_tokens: int = COMPACT_TARGET_TOKENS,
        recent_tool_results_tokens: int = RECENT_TOOL_RESULTS_TOKENS,
        large_output_threshold: int = LARGE_OUTPUT_THRESHOLD,
        preview_max_chars: int | None = None,
        compact_prompt_template: str | None = None,
        compact_merge_prompt_template: str | None = None,
        artifact_store: Any | None = None,
    ) -> None:
        super().__init__(
            model=model,
            cache_dir=cache_dir,
            max_context_tokens=max_context_tokens,
            micro_compact_tokens=micro_compact_tokens,
            compact_target_tokens=compact_target_tokens,
            recent_tool_results_tokens=recent_tool_results_tokens,
            large_output_threshold=large_output_threshold,
            preview_max_chars=preview_max_chars,
            compact_prompt_template=compact_prompt_template,
            compact_merge_prompt_template=compact_merge_prompt_template,
            artifact_store=artifact_store,
        )
        self.session_id = session_id
        self._memory_store = memory_store or FileMemoryStore(
            root_dir=str(Path(cache_dir).parent / ".agent_memory") if cache_dir else ".agent_memory"
        )
        # Working memory snapshot (last compacted view) used by retrieval tier.
        self._working_summary: str | None = None

    # -- Session tier ---------------------------------------------------------

    async def session_get(self, key: str) -> Any | None:
        """Retrieve a value from the current session namespace."""
        return await self._memory_store.get(key, namespace=self._session_ns())

    async def session_set(self, key: str, value: Any) -> None:
        """Store a value in the current session namespace."""
        await self._memory_store.set(key, value, namespace=self._session_ns())

    async def session_delete(self, key: str) -> None:
        await self._memory_store.delete(key, namespace=self._session_ns())

    async def session_keys(self) -> list[str]:
        return await self._memory_store.list_keys(namespace=self._session_ns())

    # -- Long-term tier -------------------------------------------------------

    async def long_term_get(self, key: str) -> Any | None:
        """Retrieve a value from persistent long-term memory."""
        return await self._memory_store.get(key, namespace="long_term")

    async def long_term_set(self, key: str, value: Any) -> None:
        """Persist a value across sessions."""
        await self._memory_store.set(key, value, namespace="long_term")

    async def long_term_delete(self, key: str) -> None:
        await self._memory_store.delete(key, namespace="long_term")

    async def long_term_keys(self) -> list[str]:
        return await self._memory_store.list_keys(namespace="long_term")

    # -- Retrieval tier -------------------------------------------------------

    async def retrieve(self, query: MemoryQuery) -> list[MemoryRecall]:
        """Recall relevant memories from session and long-term tiers.

        The default implementation uses a simple keyword relevance score.
        It is intentionally replaceable: inject a MemoryStore subclass with
        a vector/search backend to upgrade recall quality without changing
        callers.
        """
        terms = _extract_query_terms(query.text)
        results: list[MemoryRecall] = []

        tiers: list[MemoryTier] = [query.tier] if query.tier else ["session", "long_term"]
        for tier in tiers:
            ns = self._session_ns() if tier == "session" else "long_term"
            keys = await self._memory_store.list_keys(namespace=ns)
            for key in keys:
                value = await self._memory_store.get(key, namespace=ns)
                if value is None:
                    continue
                score = self._score_memory(terms, key, value)
                if score <= 0:
                    continue
                meta = {"session_id": self.session_id} if tier == "session" else {}
                results.append(
                    MemoryRecall(
                        key=key,
                        value=value,
                        tier=tier,
                        score=score,
                        meta=meta,
                    )
                )

        # If requested, also search working-memory summary.
        if query.tier is None or query.tier == "working":
            if self._working_summary:
                score = self._score_memory(terms, "working_summary", self._working_summary)
                if score > 0:
                    results.append(
                        MemoryRecall(
                            key="working_summary",
                            value=self._working_summary,
                            tier="working",
                            score=score,
                        )
                    )

        results.sort(key=lambda r: r.score, reverse=True)
        return results[: query.top_k]

    async def working_recall(self, messages: Sequence[Message], query: str) -> list[MemoryRecall]:
        """Convenience: retrieve against the current working memory context."""
        summary = _build_summary(tuple(messages))
        self._working_summary = summary
        return await self.retrieve(MemoryQuery(text=query, top_k=5))

    # -- Working tier (inherited from ContextManager) -------------------------

    async def compact_if_needed(
        self,
        messages: tuple[Message, ...],
        *,
        on_compact_start: Callable[[], Awaitable[None]] | None = None,
    ) -> tuple[Message, ...]:
        """Layer-3 compaction, updating the working-memory summary."""
        compacted = await super().compact_if_needed(messages, on_compact_start=on_compact_start)
        self._working_summary = _build_summary(compacted)
        return compacted

    # -- Helpers --------------------------------------------------------------

    def _session_ns(self) -> str:
        return f"session_{self.session_id}"

    @staticmethod
    def _score_memory(terms: set[str], key: str, value: Any) -> float:
        """Simple keyword overlap score."""
        haystack = f"{key} {json.dumps(value, ensure_ascii=False, default=str)}".lower()
        if not terms:
            return 0.0
        matches = sum(1 for term in terms if term in haystack)
        return matches / len(terms)


def _extract_query_terms(text: str) -> set[str]:
    """Split a retrieval query into matchable terms (CJK-aware).

    Non-CJK segments keep the whitespace-token semantics: runs longer than
    one character, lowercased.  CJK runs have no delimiters, so the old
    whitespace split turned a whole Chinese query into one term requiring an
    exact contiguous substring; runs of CJK characters are now split into
    overlapping bigrams ("季度报告" → {季度, 度报, 报告}) so partial overlaps
    score proportionally.  A single CJK character stays as a one-char term.
    """
    terms: set[str] = set()
    for chunk in text.split():
        for is_cjk, group in groupby(chunk, _is_cjk):
            run = "".join(group)
            if is_cjk:
                if len(run) >= 2:
                    terms.update(run[i : i + 2] for i in range(len(run) - 1))
                else:
                    terms.add(run)
            elif len(run) > 1:
                terms.add(run.lower())
    return terms
