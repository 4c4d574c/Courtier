"""MemoryManager — file-based memory with per-turn recall injection.

Memory is a convention over universal file tools (read/edit/write):
plain files under the memory workspace plus MEMORY.md indexes that the
model maintains (docs/architecture/memory-file-tools-plan.md).  What
remains here is the working-memory engine inherited from ContextManager
plus the recall injection: once per real user turn, think_phase calls
``inject_memory_recall`` (duck-typed) to prepend the relevant index
files as a hint message.
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
    "[Recalled memory] The notes below are the current memory indexes:\n"
    "{memories}\nUse the read / write / edit tools to work with memory files."
)


class MemoryManager(ContextManager):
    """ContextManager + recall injection over file-based memory.

    The memory workspace root is ``<cache_dir>/../.agent_memory`` with
    ``common/MEMORY.md`` for domain-agnostic notes and
    ``<domain>/MEMORY.md`` per active domain.  Active domains are seeded
    by the host (build_agent) and extended by the DomainActivator via
    :meth:`note_domain_active`.
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
        self._active_domains: set[str] = set()
        # id() of the hint message this manager injected for the current
        # turn — structural dedup, no content matching.
        self._last_recall_hint_id: int | None = None

    # -- Memory workspace ------------------------------------------------------

    @property
    def memory_home(self) -> Path:
        """Root of the global file-based memory workspace."""
        return Path(self._cache_dir).parent / ".agent_memory"

    @property
    def session_workspace(self) -> Path:
        """This session's scratch workspace (a permission-gate root too)."""
        return Path(self._cache_dir).parent / ".agent_sessions" / self.session_id

    def note_domain_active(self, domain: str) -> None:
        """Register an active domain; its index joins the recall injection."""
        self._active_domains.add(domain)

    # -- Forking ---------------------------------------------------------------

    def fork(self) -> "MemoryManager":
        """Fork for sub-agents, keeping the file-based memory injection.

        Shares the parent's memory workspace (sub-agent findings land in
        files the orchestrator can read) and its active-domain snapshot;
        the CompactState starts fresh as with ContextManager.fork().
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
        )
        child._active_domains = set(self._active_domains)
        return child

    # -- Recall injection (called by think_phase, duck-typed) -------------------

    async def inject_memory_recall(
        self, messages: tuple["Any", ...]
    ) -> tuple["Any", ...]:
        """Prepend the relevant memory indexes as a hint after the turn's
        genuine user message.

        Runs once per real user turn: the hint (source="hint") is inserted
        right after the last genuine user message and this message's id
        suppresses re-injection for the rest of the turn.  Sources are the
        common index plus the indexes of active domains, each capped and
        the total capped.  No-ops when disabled, when there is no genuine
        user message, when this turn already carries the hint, or when no
        index has content (zero cost).
        """
        if not self._auto_inject_enabled:
            return messages
        from .context_manager import _find_last_real_user_index

        idx = _find_last_real_user_index(messages)
        if idx is None:
            return messages
        hint_id = self._last_recall_hint_id
        if hint_id is not None and any(id(m) == hint_id for m in messages[idx + 1 :]):
            return messages

        # The workspace paths are dynamic per deployment — without this
        # segment the model has to guess them and lose a call to the gate
        # (observed live).  Always present when injection is on.
        segments = [
            (
                f"【记忆工作区】\n- 全局记忆根: {self.memory_home}\n"
                f"- 本会话工作区: {self.session_workspace}\n"
                "- 约定：common/ 存通用记忆，<领域>/ 存领域记忆；"
                "每个目录的 MEMORY.md 是索引，写入或修改记忆后同步更新"
            )
        ]
        segments += self._collect_index_segments()

        template = self._recall_hint_template or _FALLBACK_RECALL_HINT
        hint = Message(
            role="user",
            content=template.replace("{memories}", "\n\n".join(segments)),
            source="hint",
        )
        self._last_recall_hint_id = id(hint)
        return (*messages[: idx + 1], hint, *messages[idx + 1 :])

    def _collect_index_segments(self) -> list[str]:
        """Read common + active-domain indexes, capped per index and in total."""
        home = self.memory_home
        sources: list[tuple[str, Path]] = [("common", home / "common")]
        sources += [(domain, home / domain) for domain in sorted(self._active_domains)]

        segments: list[str] = []
        total = 0
        for name, root in sources:
            index = root / "MEMORY.md"
            try:
                if not index.is_file():
                    continue
                text = index.read_text(encoding="utf-8").strip()
            except OSError:
                logger.warning("Failed to read memory index %s", index, exc_info=True)
                continue
            if not text:
                continue
            if len(text) > self._auto_inject_max_chars:
                text = text[: self._auto_inject_max_chars] + "…"
            segment = f"【{name} 记忆索引】\n{text}"
            if total + len(segment) > self._auto_inject_total_chars:
                break
            segments.append(segment)
            total += len(segment)
        return segments
