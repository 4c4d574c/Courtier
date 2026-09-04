"""MemoryManager — DB-backed layered memory with per-turn recall injection.

Memory entries live in the host database (global shared layer + per-user
layers, both split into ``common`` and per-domain groups) and are reached
through the ``memory`` plugin tool.  What remains here is the
working-memory engine inherited from ContextManager plus the recall
injection: once per real user turn, think_phase calls
``inject_memory_recall`` (duck-typed) to prepend the caller's memory
index — fetched through a host-injected async provider — as a hint
message.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .context_manager import (
    COMPACT_TARGET_TOKENS,
    LARGE_OUTPUT_THRESHOLD,
    MAX_CONTEXT_TOKENS,
    MICRO_COMPACT_TOKENS,
    RECENT_TOOL_RESULTS_TOKENS,
    ContextManager,
)
from .state import Message

if TYPE_CHECKING:

    from .model import ModelClient

logger = logging.getLogger(__name__)

MemoryTier = str  # kept for annotation compatibility with older callers

# Minimal en-US protocol fallback used when no PromptBundle provides
# context.memory_recall_hint (bare test environments — same pattern as the
# compact prompt fallbacks in context_manager.py).
_FALLBACK_RECALL_HINT = (
    "[Recalled memory] Below is your memory index (user layer first):\n"
    "{memories}\n"
    "Use the memory tool to list / read / write / delete entries."
)


class MemoryManager(ContextManager):
    """ContextManager + recall injection over DB-backed layered memory.

    The host (build_agent) injects an async ``memory_index_provider`` —
    ``domains: list[str] -> {"user": [entry], "global": [entry]}`` —
    resolving the caller's user layer and the global shared layer.
    Injection covers ``common`` entries plus the entries of the session's
    active domains (seeded by the host, extended by the DomainActivator
    via :meth:`note_domain_active`).  No provider (anonymous session or
    memory plugin absent) means no injection at all.
    """

    def __init__(
        self,
        model: "ModelClient | None" = None,
        cache_dir: str = ".agent_cache",
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
        media_image_tokens: int = 1024,
        media_audio_tokens_per_second: float = 40.0,
        media_video_tokens_per_second: float = 200.0,
        memory_auto_inject_enabled: bool = True,
        memory_auto_inject_max_chars: int = 400,
        memory_auto_inject_total_chars: int = 1500,
        memory_recall_hint_template: str | None = None,
        memory_index_provider: Any | None = None,
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
            media_image_tokens=media_image_tokens,
            media_audio_tokens_per_second=media_audio_tokens_per_second,
            media_video_tokens_per_second=media_video_tokens_per_second,
        )
        self.session_id = session_id
        self._auto_inject_enabled = memory_auto_inject_enabled
        self._auto_inject_max_chars = max(1, memory_auto_inject_max_chars)
        self._auto_inject_total_chars = max(1, memory_auto_inject_total_chars)
        self._recall_hint_template = memory_recall_hint_template
        self._memory_index_provider = memory_index_provider
        self._active_domains: set[str] = set()
        # id() of the hint message this manager injected for the current
        # turn — structural dedup, no content matching.
        self._last_recall_hint_id: int | None = None

    # -- Memory workspace ------------------------------------------------------

    @property
    def session_workspace(self) -> Path:
        """This session's scratch workspace (a permission-gate root too)."""
        return Path(self._cache_dir).parent / ".agent_sessions" / self.session_id

    def note_domain_active(self, domain: str) -> None:
        """Register an active domain; its memory entries join the recall injection."""
        self._active_domains.add(domain)

    # -- Forking ---------------------------------------------------------------

    def fork(self) -> "MemoryManager":
        """Fork for sub-agents, keeping the memory recall injection.

        Shares the parent's provider and active-domain snapshot; the
        CompactState starts fresh as with ContextManager.fork().
        """
        child = MemoryManager(
            model=self._model,
            cache_dir=str(self._cache_dir),
            session_id=self.session_id,
            max_context_tokens=self.max_context_tokens,
            micro_compact_tokens=self.micro_compact_tokens,
            compact_target_tokens=self.compact_target_tokens,
            recent_tool_results_tokens=self.recent_tool_results_tokens,
            large_output_threshold=self.large_output_threshold,
            preview_max_chars=None,
            compact_prompt_template=self._compact_prompt_template,
            compact_merge_prompt_template=self._compact_merge_prompt_template,
            artifact_store=self._cache,
            memory_auto_inject_enabled=self._auto_inject_enabled,
            memory_auto_inject_max_chars=self._auto_inject_max_chars,
            memory_auto_inject_total_chars=self._auto_inject_total_chars,
            memory_recall_hint_template=self._recall_hint_template,
            memory_index_provider=self._memory_index_provider,
        )
        child._active_domains = set(self._active_domains)
        return child

    # -- Recall injection (called by think_phase, duck-typed) -------------------

    async def inject_memory_recall(
        self, messages: tuple["Any", ...]
    ) -> tuple["Any", ...]:
        """Prepend the caller's memory index as a hint after the turn's
        genuine user message.

        Runs once per real user turn: the hint (source="hint") is inserted
        right after the last genuine user message and this message's id
        suppresses re-injection for the rest of the turn.  The index is
        fetched through the host-injected provider (user layer first, then
        global; ``common`` entries plus the active domains'), each entry
        capped and the total capped.  No-ops when disabled, when no
        provider is wired (anonymous / plugin absent), when there is no
        genuine user message, when this turn already carries the hint, or
        when the index comes back empty (zero cost).
        """
        if not self._auto_inject_enabled or self._memory_index_provider is None:
            return messages
        from .context_manager import _find_last_real_user_index

        idx = _find_last_real_user_index(messages)
        if idx is None:
            return messages
        hint_id = self._last_recall_hint_id
        if hint_id is not None and any(id(m) == hint_id for m in messages[idx + 1 :]):
            return messages

        segments = await self._collect_index_segments()
        if not segments:
            return messages

        template = self._recall_hint_template or _FALLBACK_RECALL_HINT
        hint = Message(
            role="user",
            content=template.replace("{memories}", "\n\n".join(segments)),
            source="hint",
        )
        self._last_recall_hint_id = id(hint)
        return (*messages[: idx + 1], hint, *messages[idx + 1 :])

    async def _collect_index_segments(self) -> list[str]:
        """Fetch the caller's index via the provider and shape it into
        labeled segments, capped per entry and in total."""
        try:
            grouped = self._memory_index_provider(sorted(self._active_domains))
            if hasattr(grouped, "__await__"):
                grouped = await grouped
        except Exception:
            # fail-open: an index hiccup must never break the turn.
            logger.warning("memory index provider failed", exc_info=True)
            return []

        segments: list[str] = []
        total = 0
        for label, entries in (
            ("用户记忆", grouped.get("user")),
            ("全局共享记忆", grouped.get("global")),
        ):
            if not entries:
                continue
            lines = []
            for entry in entries:
                title = str(entry.get("title") or "").strip()
                domain = str(entry.get("domain") or "common").strip() or "common"
                preview = str(entry.get("content") or "").strip()
                if len(preview) > self._auto_inject_max_chars:
                    preview = preview[: self._auto_inject_max_chars] + "…"
                suffix = f"（{domain}）" if domain != "common" else ""
                lines.append(f"- {title}{suffix}：{preview}" if title else f"- {preview}")
            if not lines:
                continue
            segment = f"【{label}】\n" + "\n".join(lines)
            if total + len(segment) > self._auto_inject_total_chars:
                break
            segments.append(segment)
            total += len(segment)
        return segments
