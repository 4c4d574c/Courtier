"""ContextManager — three-layer context budget control for the agent loop.

Layer 1: Large tool outputs → persisted to disk, preview in context.
Layer 2: Old tool results → replaced with placeholders (budget-gated,
         keeps the most recent results within a token budget).
Layer 3: Full compaction → LLM summarizes older history when the token
         budget is exceeded.

All budget decisions are token-based (CJK-aware heuristic) and calibrated
with the provider-reported ``prompt_tokens`` after every model call.

Learning from: https://learn.shareai.run/zh/s06/
"""

from __future__ import annotations

import json
import logging
import time
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..telemetry.metrics import (
    record_context_compaction,
    record_context_compaction_duration,
)
from .state import Message

if TYPE_CHECKING:
    from .model import ModelClient

logger = logging.getLogger(__name__)

# Token budgets are derived from the model's context window (default 32k) by
# agent_service; the module-level values below are the standalone defaults.
MAX_CONTEXT_TOKENS = 24_576  # 0.75 × 32768 — trigger full compaction
MICRO_COMPACT_TOKENS = 19_660  # 0.60 × 32768 — enable micro-compact above this
COMPACT_TARGET_TOKENS = 16_384  # 0.50 × 32768 — target after compaction
RECENT_TOOL_RESULTS_TOKENS = 4_000  # micro-compact keeps recent results within this
MIN_RECENT_TOOL_RESULTS = 2  # always keep at least this many recent tool results
LARGE_OUTPUT_THRESHOLD = 3_000  # persist to disk when tool result exceeds this (chars)
COMPACT_STATE_VERSION = 1  # version of the serialised CompactState payload

# Minimal en-US protocol fallback used when the domain PromptBundle does not
# provide context.compact_prompt / context.compact_merge_prompt (or no
# template was injected, e.g. bare tests).  The line containing only ``---``
# separates the system body from the user instruction.
_FALLBACK_COMPACT_PROMPT = (
    "You are a context compaction assistant. Compress the conversation "
    "history below into a compact summary. You MUST preserve:\n"
    "1. Current task goal\n"
    "2. Completed key operations\n"
    "3. Files involved\n"
    "4. Key decisions and constraints\n"
    "5. Next concrete action\n"
    "6. All available $ref IDs and file paths\n\n"
    "Conversation history:\n{history}\n---\n"
    "Compress the history above. Output only the summary."
)
_FALLBACK_COMPACT_MERGE_PROMPT = (
    "You are a context compaction assistant. Merge the NEW SEGMENT into the "
    "EXISTING SUMMARY and output the updated full summary. Preserve the same "
    "six information categories (including all $ref IDs and file paths).\n\n"
    "EXISTING SUMMARY:\n{previous_summary}\n\n"
    "NEW SEGMENT:\n{new_segment}\n---\n"
    "Merge and output the updated summary."
)


@dataclass
class CompactState:
    """Tracks compaction status across the agent loop."""

    has_compacted: bool = False
    last_summary: str | None = None
    compact_count: int = 0
    # True when the latest compaction could not bring the context back under
    # budget (the system prompt itself dominates).  Surfaced to the frontend
    # via the compact step event.
    last_compact_over_budget: bool = False


class ContextManager:
    """Three-layer context budget management.

    Usage:
        mgr = ContextManager(model=model_client, cache_dir=".agent_cache")
        # Layer 1 (large tool outputs → disk) runs inside ToolRegistry.execute
        # via ArtifactStore.persist — there is no manager-level wrapper.
        # Layer 2: after adding observation
        messages = mgr.micro_compact(messages)
        # Layer 3: before next think
        messages = await mgr.compact_if_needed(messages)
    """

    def __init__(
        self,
        model: ModelClient | None = None,
        cache_dir: str = ".agent_cache",
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
        self._model = model
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self.max_context_tokens = max_context_tokens
        self.micro_compact_tokens = micro_compact_tokens
        self.compact_target_tokens = compact_target_tokens
        self.recent_tool_results_tokens = recent_tool_results_tokens
        self.large_output_threshold = large_output_threshold
        self._compact_prompt_template = compact_prompt_template
        self._compact_merge_prompt_template = compact_merge_prompt_template
        self.state = CompactState()
        # Provider-reported prompt tokens from the latest model call; used to
        # calibrate the heuristic estimate (never trigger late).
        self._last_actual_prompt_tokens: int | None = None
        # Unified store: everything goes through an ArtifactStore (which
        # subsumes the legacy CacheStore).  Duck-typing works because
        # ArtifactStore provides persist, resolve_refs, ref_map, and set_ref.
        if artifact_store is None:
            from ..artifacts.store import ArtifactStore

            store_kwargs: dict[str, Any] = {
                "cache_dir": str(self._cache_dir),
                "large_output_threshold": large_output_threshold,
            }
            if preview_max_chars is not None:
                store_kwargs["preview_max_chars"] = preview_max_chars
            artifact_store = ArtifactStore(**store_kwargs)
        self._cache = artifact_store

    # -- Ref resolution -------------------------------------------------------

    def resolve_refs(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        """Recursively resolve $ref:tool:N references in tool call arguments.

        Returns a new dict with ref strings replaced by data loaded from disk.
        Unknown refs and missing files are kept as-is with a warning log.
        """
        return self._cache.resolve_refs(kwargs)

    # -- State persistence ----------------------------------------------------

    def snapshot_state(self) -> dict[str, Any]:
        """Serialise CompactState for session persistence.

        Stored alongside the artifact snapshot in the session JSON so the
        ``has_compacted`` re-compaction guard and the ``compact_count``
        numbering survive across requests.
        """
        return {
            "version": COMPACT_STATE_VERSION,
            "has_compacted": self.state.has_compacted,
            "last_summary": self.state.last_summary,
            "compact_count": self.state.compact_count,
        }

    def load_state(self, state: dict[str, Any] | None) -> None:
        """Restore CompactState from a session payload.

        A missing or unknown-version payload leaves the default fresh state —
        this is default initialisation, not a compatibility shim.
        """
        if not isinstance(state, dict) or not state:
            return
        if state.get("version") != COMPACT_STATE_VERSION:
            logger.warning(
                "Unknown compact state version %r; starting fresh",
                state.get("version"),
            )
            return
        self.state.has_compacted = bool(state.get("has_compacted", False))
        self.state.last_summary = state.get("last_summary")
        self.state.compact_count = int(state.get("compact_count", 0))

    # -- Forking ---------------------------------------------------------------

    def fork(self) -> "ContextManager":
        """Return a child manager sharing the cache store and model.

        Sub-agents get an independent ``CompactState`` so their compactions
        do not interleave with the parent's ``has_compacted`` guard and
        ``compact_count`` numbering.  Always returns a plain
        ``ContextManager``, even when called on a subclass.
        """
        return ContextManager(
            model=self._model,
            cache_dir=str(self._cache_dir),
            max_context_tokens=self.max_context_tokens,
            micro_compact_tokens=self.micro_compact_tokens,
            compact_target_tokens=self.compact_target_tokens,
            recent_tool_results_tokens=self.recent_tool_results_tokens,
            large_output_threshold=self.large_output_threshold,
            compact_prompt_template=self._compact_prompt_template,
            compact_merge_prompt_template=self._compact_merge_prompt_template,
            artifact_store=self._cache,
        )

    # -- Layer 2: Micro-compact old tool results ------------------------------

    async def micro_compact(
        self,
        messages: tuple[Message, ...],
        *,
        force: bool = False,
    ) -> tuple[Message, ...]:
        """Replace old tool-result messages with placeholders.

        Budget-gated: while the estimated usage is below
        ``micro_compact_tokens`` the history is left intact (only reminder
        deduplication runs).  Above the gate, the most recent tool results
        are kept within ``recent_tool_results_tokens`` (at least
        ``MIN_RECENT_TOOL_RESULTS``); older ones become omission markers
        with a ref_id for recovery.  If the result was already persisted by
        Layer 1, the existing ref_id is reused; otherwise it is persisted
        to disk first.

        *force* skips the budget gate (explicit compaction requests, e.g.
        the plugin host service).

        System and user messages are always preserved.
        """
        # Reminder dedup runs on every call regardless of budget pressure —
        # the loop injects reminders every turn and they would otherwise
        # accumulate linearly.
        deduped = tuple(_deduplicate_reminders(list(messages)))

        if not force and self.budget_usage(deduped) < self.micro_compact_tokens:
            return deduped

        tool_indices = [i for i, m in enumerate(deduped) if m.role == "tool"]
        keep = self._select_recent_tool_indices(deduped, tool_indices)
        to_compact = set(tool_indices) - keep
        if not to_compact:
            return deduped

        compacted = []
        for i, msg in enumerate(deduped):
            if i in to_compact:
                tool_name = msg.name or f"tool_call_{msg.tool_call_id}"

                ref_id, ref_file, success = self._extract_ref_info(msg)
                if ref_id is None:
                    ref_id, ref_file, persist_success = await self._persist_tool_message(
                        msg, tool_name
                    )
                    if success is None:
                        success = persist_success

                note = "结果已被微压缩省略（旧工具结果）。" "将 $ref 作为工具参数即可恢复完整数据。"
                placeholder: dict[str, Any] = {
                    "_omitted": True,
                    "tool": tool_name,
                    "note": note,
                }
                # Keep the success flag so post-compaction checks (e.g. the
                # parse-before-anything guard) can still tell whether this
                # call succeeded without recovering the full payload.
                if success is not None:
                    placeholder["success"] = success
                if ref_id:
                    placeholder["ref_id"] = ref_id
                if ref_file:
                    # Debug info only — restore goes through the artifact
                    # snapshot, not through this marker.
                    placeholder["file"] = ref_file

                compacted.append(
                    Message(
                        role="tool",
                        content=json.dumps(placeholder, ensure_ascii=False),
                        tool_call_id=msg.tool_call_id,
                        name=tool_name,
                    )
                )
            else:
                compacted.append(msg)

        record_context_compaction("micro")
        return tuple(compacted)

    def _select_recent_tool_indices(
        self, messages: tuple[Message, ...], tool_indices: list[int]
    ) -> set[int]:
        """Select the most recent tool results that fit the token budget.

        Walks tool messages from newest to oldest, keeping results while
        budget remains — but always keeps at least
        ``MIN_RECENT_TOOL_RESULTS`` so the newest exchanges survive even
        when a single result exceeds the budget on its own.
        """
        kept: list[int] = []
        budget = self.recent_tool_results_tokens
        for i in reversed(tool_indices):
            cost = self.estimate_tokens((messages[i],))
            if len(kept) >= MIN_RECENT_TOOL_RESULTS and cost > budget:
                break
            kept.append(i)
            budget -= cost
        return set(kept)

    # -- Budget helpers ----------------------------------------------------------

    def update_actual_usage(self, prompt_tokens: int | None) -> None:
        """Record the provider-reported prompt token count.

        Called after every model response; budget checks take
        ``max(heuristic, actual)`` so compaction never triggers late when
        the heuristic underestimates.
        """
        if prompt_tokens and prompt_tokens > 0:
            self._last_actual_prompt_tokens = prompt_tokens

    def budget_usage(self, messages: tuple[Message, ...]) -> int:
        """Estimated context usage in tokens, calibrated by actual usage."""
        estimate = self.estimate_tokens(messages)
        if self._last_actual_prompt_tokens is not None:
            return max(estimate, self._last_actual_prompt_tokens)
        return estimate

    def _extract_ref_info(self, msg: Message) -> tuple[str | None, str | None, bool | None]:
        """Extract (ref_id, file, success) from a persisted tool message.

        Handles both persistence shapes:
        - Layer-1 marker: ``raw_data.__persisted_output__`` (registry-level
          persist of small-marker payloads);
        - Summarizer path: ``raw_data`` was dropped by ResultSummarizer and
          the ref lives at top-level ``result_id`` (falling back to
          ``metadata.stored.result_id`` / ``metadata.persisted_ref_id``).

        Returns (None, None, success) when the message carries no ref; the
        success flag is read from the tool-result envelope either way.
        """
        if not msg.content:
            return None, None, None
        try:
            payload = json.loads(msg.content)
        except (json.JSONDecodeError, TypeError):
            return None, None, None
        if not isinstance(payload, dict):
            return None, None, None
        success = payload.get("success")
        success = success if isinstance(success, bool) else None

        data = payload.get("raw_data")
        if isinstance(data, dict) and data.get("__persisted_output__") is True:
            return data.get("ref_id"), data.get("file"), success

        ref_id = payload.get("result_id")
        if not ref_id:
            metadata = payload.get("metadata")
            if isinstance(metadata, dict):
                stored = metadata.get("stored")
                if isinstance(stored, dict) and stored.get("result_id"):
                    ref_id = stored["result_id"]
                elif metadata.get("persisted_ref_id"):
                    ref_id = metadata["persisted_ref_id"]
        if not ref_id:
            return None, None, success

        # The file path is debug-only info; look it up best-effort from the
        # store rather than trusting the message to carry it.
        ref_file: str | None = None
        get_info = getattr(self._cache, "get_info", None)
        if callable(get_info):
            info = get_info(ref_id)
            if info:
                ref_file = info.get("file")
        return ref_id, ref_file, success

    async def _persist_tool_message(
        self, msg: Message, tool_name: str
    ) -> tuple[str | None, str | None, bool | None]:
        """Persist a non-persisted tool message to disk; return (ref_id, file, success).

        Extracts the inner payload from the tool result envelope
        ({"success": ..., "raw_data": ..., "error": ...}) and persists it.
        Returns (None, None, success) if the message has no data to persist.
        """
        if not msg.content:
            return None, None, None
        try:
            payload = json.loads(msg.content)
            data = payload.get("raw_data")
            success = payload.get("success")
            success = success if isinstance(success, bool) else None
            if data is None:
                return None, None, success
        except (json.JSONDecodeError, TypeError):
            return None, None, None

        result = await self._cache.persist(data, tool_name, force=True)
        if not result.persisted:
            return None, None, success  # OSError or empty ref_id — caller checks ref_id

        ref_file = result.data.get("file") if isinstance(result.data, dict) else None
        logger.debug(
            "Micro-compact persisted: %s -> %s",
            tool_name,
            result.ref_id,
        )
        return result.ref_id, ref_file, success

    # -- Layer 3: Full compaction ---------------------------------------------

    async def compact_if_needed(
        self,
        messages: tuple[Message, ...],
        *,
        on_compact_start: Callable[[], Awaitable[None]] | None = None,
    ) -> tuple[Message, ...]:
        """If context exceeds budget, summarize the conversation history.

        Returns a compacted message tuple with a summary replacing old messages.
        *on_compact_start* fires once compaction is actually triggered (the
        budget is exceeded) but before the slow LLM summarization begins —
        consumers use it to show a "compacting" indicator.
        """
        usage = self.budget_usage(messages)
        if usage < self.max_context_tokens:
            return messages

        logger.info(
            "Context budget exceeded (%d tokens), triggering full compaction",
            usage,
        )
        if on_compact_start is not None:
            await on_compact_start()
        return await self._full_compact(messages)

    async def force_compact(self, messages: tuple[Message, ...]) -> tuple[Message, ...]:
        """Force a full compaction regardless of budget (e.g., /compact command).

        Unlike budget-triggered compaction this compresses everything,
        including the current turn — the user explicitly asked for it.
        """
        logger.info("Forced compaction requested")
        return await self._full_compact(messages, force_all=True)

    async def _full_compact(
        self, messages: tuple[Message, ...], *, force_all: bool = False
    ) -> tuple[Message, ...]:
        """Summarize the history BEFORE the current turn; keep the turn intact.

        Partial compaction: the boundary is the last genuine user message
        (reminders excluded).  Everything from the boundary on — the current
        turn's assistant/tool exchanges — is preserved verbatim; only the
        pre-boundary history is summarized.  With *force_all* the boundary
        is the end of the conversation (explicit user request).
        """
        # --- Locate the current-turn boundary ---
        if force_all:
            boundary = len(messages)
        else:
            real_user_idx = _find_last_real_user_index(messages)
            boundary = real_user_idx if real_user_idx is not None else len(messages)

        # The first message counts as the preservable system prompt only when
        # it is not itself a previous compaction summary (a summary belongs
        # to the compressible history so the incremental merge can find it).
        head_start = (
            1
            if (messages and messages[0].role == "system" and not _is_summary_message(messages[0]))
            else 0
        )
        history_segment = messages[head_start:boundary]

        if not history_segment:
            # Nothing before the current turn to summarize.  If we got here,
            # the current turn alone exceeds the budget — Layer 2
            # (micro-compact) is the only remaining remedy.
            self.state.last_compact_over_budget = True
            logger.error(
                "Context over budget with no compressible history "
                "(system prompt / current turn dominates)"
            )
            return messages

        if (
            not force_all
            and self.state.has_compacted
            and all(_is_summary_message(m) for m in history_segment)
        ):
            # Already compacted and no new history accumulated before the
            # current turn — summarizing again would only degrade the summary.
            logger.warning("Nothing new to compact before the current turn; skipping")
            return messages

        if self._model is None:
            logger.warning("Cannot compact: no model configured")
            record_context_compaction("failed")
            return messages

        # --- Build the summarization input ---
        # Incremental: when a previous summary exists, merge only the new
        # segment into it instead of re-summarizing everything.  Prompt text
        # comes from the domain PromptBundle (context.compact_prompt /
        # context.compact_merge_prompt); the en-US protocol fallback keeps
        # bare environments working.
        prev_summary: str | None = None
        new_segment: tuple[Message, ...] = history_segment
        if (
            self.state.has_compacted
            and self.state.last_summary
            and history_segment
            and _is_summary_message(history_segment[0])
        ):
            prev_summary = self.state.last_summary
            new_segment = history_segment[1:]

        if prev_summary and new_segment:
            template = self._compact_merge_prompt_template or _FALLBACK_COMPACT_MERGE_PROMPT
            prompt_body = template.replace("{previous_summary}", prev_summary).replace(
                "{new_segment}", _build_summary(new_segment)
            )
        else:
            template = self._compact_prompt_template or _FALLBACK_COMPACT_PROMPT
            prompt_body = template.replace("{history}", _build_summary(history_segment))
        system_body, user_instruction = _split_compact_template(prompt_body)

        compact_messages: list[dict] = [
            {"role": "system", "content": system_body},
            {"role": "user", "content": user_instruction},
        ]

        try:
            compact_start = time.perf_counter()
            response = await self._model.generate(compact_messages)
            record_context_compaction_duration(time.perf_counter() - compact_start)
            summary = response.content or system_body[:500]
        except Exception as exc:
            logger.warning("Compaction summary failed: %s, using fallback", exc)
            record_context_compaction("fallback")
            # Keep the most recent messages instead of truncating to 800 chars,
            # which would lose almost all context.  8 recent messages preserve
            # enough conversational context to recover gracefully.
            keep_recent = min(8, len(messages))
            fallback_compacted = list(messages[-keep_recent:])
            # Align to tool-call boundaries: an arbitrary slice can start mid
            # tool-call sequence (leading orphan tool messages) or contain an
            # assistant whose tool results fell outside the slice (dangling
            # tool_calls) — both are rejected by OpenAI-compatible APIs.
            fallback_compacted = _align_tool_boundaries(fallback_compacted)
            # Truncate message content to avoid context overflow
            _MAX_FALLBACK_CONTENT = 4000
            for i, msg in enumerate(fallback_compacted):
                if msg.content and len(msg.content) > _MAX_FALLBACK_CONTENT:
                    # Message is frozen — build a truncated copy instead of
                    # mutating in place (the old assignment raised
                    # FrozenInstanceError at runtime).
                    fallback_compacted[i] = replace(
                        msg,
                        content=msg.content[:_MAX_FALLBACK_CONTENT] + "...[truncated]",
                    )
            self.state.has_compacted = True
            self.state.last_summary = f"[压缩失败，回退到最近 {keep_recent} 条消息]"
            self.state.compact_count += 1
            self.state.last_compact_over_budget = (
                self.estimate_tokens(tuple(fallback_compacted)) > self.max_context_tokens
            )
            logger.info(
                "Compaction fallback: %d → %d messages",
                len(messages),
                len(fallback_compacted),
            )
            return tuple(fallback_compacted)

        self.state.has_compacted = True
        self.state.compact_count += 1
        record_context_compaction("full")

        # --- Assemble: [system prompt?, summary, current turn...] ---
        compacted: list[Message] = []

        # Preserve the original system prompt when it exists.
        if head_start:
            compacted.append(messages[0])

        summary_prefix = (
            f"[上下文压缩 #{self.state.compact_count}] "
            "以下为之前对话的摘要，请继续完成任务：\n\n"
        )
        summary_idx = len(compacted)
        compacted.append(Message(role="system", content=summary_prefix + summary))

        # The current turn is preserved verbatim.
        compacted.extend(messages[boundary:])

        # The current turn itself can exceed the budget on its own (a burst
        # of large tool results).  Micro-compact it as a last resort — the
        # summary and system prompt are not tool messages and stay intact.
        if self.estimate_tokens(tuple(compacted)) > self.max_context_tokens:
            compacted = list(await self.micro_compact(tuple(compacted), force=True))

        # Post-compaction size check: only the summary is compressible (the
        # system prompt and the current turn must stay verbatim), so shrink
        # it when the result still exceeds the target budget.
        usage = self.estimate_tokens(tuple(compacted))
        if usage > self.compact_target_tokens:
            summary_tokens = self.estimate_tokens((compacted[summary_idx],))
            prefix_tokens = summary_tokens - self.estimate_tokens(
                (Message(role="system", content=summary),)
            )
            allowed_body = max(
                100,
                summary_tokens - (usage - self.compact_target_tokens) - prefix_tokens,
            )
            fitted = _fit_text_to_token_budget(summary, allowed_body)
            if fitted != summary:
                logger.info(
                    "Compaction summary exceeded target; truncated %d → %d chars",
                    len(summary),
                    len(fitted),
                )
                summary = fitted
                compacted[summary_idx] = replace(
                    compacted[summary_idx], content=summary_prefix + summary
                )
        self.state.last_summary = summary

        # Final check: still over budget even after shrinking the summary —
        # the system prompt itself dominates and there is nothing more to
        # compress.  Flag it so the frontend warning and metrics surface it.
        final_usage = self.estimate_tokens(tuple(compacted))
        self.state.last_compact_over_budget = final_usage > self.max_context_tokens
        if self.state.last_compact_over_budget:
            logger.error(
                "Context still over budget after compaction (%d tokens > %d budget)",
                final_usage,
                self.max_context_tokens,
            )

        logger.info(
            "Compaction complete: %d → %d messages, summary %d chars",
            len(messages),
            len(compacted),
            len(summary),
        )
        return tuple(compacted)

    # -- Helpers --------------------------------------------------------------

    def estimate_tokens(self, messages: tuple[Message, ...]) -> int:
        """Estimate token count with Chinese-aware heuristic.

        Chinese characters typically consume 1.5–2 tokens each while
        ASCII / Latin text is closer to 4 chars per token.  This
        heuristic weights CJK Unified Ideographs at ~1.5 tokens/char
        (0.65 multiplier) and everything else at ~4 chars/token
        (0.25 multiplier), giving a much more accurate budget for
        mixed Chinese–English conversations than the old ``chars/2``
        approximation.
        """
        # Scan content for per-character weighting
        cjk = 0
        other = 0
        for msg in messages:
            text = msg.content or ""
            text_cjk = sum(1 for c in text if _is_cjk(c))
            cjk += text_cjk
            other += len(text) - text_cjk
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    args_json = json.dumps(tc.arguments, ensure_ascii=False)
                    args_cjk = sum(1 for c in args_json if _is_cjk(c))
                    cjk += args_cjk
                    other += len(args_json) - args_cjk

        # Heuristic: CJK ≈ 1.5 chars/token, ASCII ≈ 4 chars/token
        return int(cjk * 0.65 + other * 0.25)

    def get_ref_map(self) -> dict[str, str]:
        """Return the persisted output ref_id -> filepath mapping."""
        return dict(self._cache.ref_map)

    def get_ref_instructions(self) -> str:
        """Return a short instruction block for the LLM about how to use ref IDs."""
        return (
            "工具结果可能保存为 $ref 缓存引用。构造下游工具参数时，"
            "优先直接把业务 $ref 传给目标工具，系统会自动加载或适配参数。\n"
            "使用 list_artifacts 查看可用的类型化工件及其元数据，"
            "使用 get_artifact 按需获取数据：其 id 参数既可填 $ref 引用，"
            "也可填 list_artifacts 输出的 artifact_id 字段值。\n"
            "当工具返回 __persisted_output__ 标记时，预览通常已足够理解结果。"
            "旧工具结果可能被微压缩为 _omitted 占位符。"
            "普通业务流程不应依赖恢复这些调试内容。"
        )


def _fit_text_to_token_budget(text: str, token_budget: int) -> str:
    """Truncate *text* so its estimated token count fits *token_budget*.

    Uses the same CJK-aware heuristic as ``ContextManager.estimate_tokens``
    and binary-searches the cut point (text is at most a few thousand chars).
    """
    if not text:
        return text

    def _est(s: str) -> int:
        cjk = sum(1 for c in s if _is_cjk(c))
        return int(cjk * 0.65 + (len(s) - cjk) * 0.25)

    if _est(text) <= token_budget:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _est(text[:mid]) <= token_budget:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + "...[truncated]"


def _is_cjk(c: str) -> bool:
    """Return True if *c* is a CJK character (Unicode Han script).

    Uses unicodedata.name() for accurate detection across all CJK blocks
    (Basic, Extension A-F, Compatibility, Supplement), replacing the previous
    narrow range check U+4E00–U+9FFF.
    """
    try:
        name = unicodedata.name(c, "")
        return name.startswith("CJK")
    except ValueError:
        return False


def _deduplicate_reminders(messages: list[Message]) -> list[Message]:
    """Remove duplicate injected reminders, keeping the last occurrence of
    each distinct reminder content.

    Reminders are tagged with ``source="reminder"`` by the agent loop.  They
    are injected at high frequency and never cleaned up by normal
    micro-compaction (which only targets tool messages), so without
    deduplication they accumulate linearly with turn count.
    """
    # Find the last occurrence of each reminder content.
    last_idx_by_content: dict[str | None, int] = {}
    for i, msg in enumerate(messages):
        if msg.role == "user" and msg.source == "reminder":
            last_idx_by_content[msg.content] = i

    if not last_idx_by_content:
        return messages

    keep_indices = set(last_idx_by_content.values())
    kept: list[Message] = []
    for i, msg in enumerate(messages):
        is_reminder = msg.role == "user" and msg.source == "reminder"
        if is_reminder and i not in keep_indices:
            continue
        kept.append(msg)

    return kept


def _find_last_real_user(
    messages: tuple[Message, ...] | list[Message],
) -> Message | None:
    """Return the most recent genuine user message.

    Reminders injected by the agent loop are user-role but tagged
    ``source="reminder"``; they must not be mistaken for the user's task.
    """
    idx = _find_last_real_user_index(messages)
    return messages[idx] if idx is not None else None


def _find_last_real_user_index(
    messages: tuple[Message, ...] | list[Message],
) -> int | None:
    """Return the index of the most recent genuine user message (or None)."""
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if m.role == "user" and m.content and m.source != "reminder":
            return i
    return None


def _is_summary_message(msg: Message) -> bool:
    """Return True when *msg* is a compaction summary written by _full_compact."""
    return msg.role == "system" and (msg.content or "").startswith("[上下文压缩 #")


def _split_compact_template(rendered: str) -> tuple[str, str]:
    """Split a rendered compact prompt into (system body, user instruction).

    Templates separate the two parts with a line containing only ``---``.
    Without a separator the whole text is the system body and a generic
    en-US instruction is used (protocol fallback, not domain text).
    """
    parts = rendered.split("\n---\n", 1)
    if len(parts) == 2:
        return parts[0], parts[1].strip()
    return rendered, "Output the summary."


def _align_tool_boundaries(messages: list[Message]) -> list[Message]:
    """Repair tool-call pairing after slicing the message history.

    A slice can start mid tool-call sequence, producing two artefacts that
    OpenAI-compatible APIs reject with a 400:

    1. Leading orphan ``role="tool"`` messages whose assistant (carrying the
       matching ``tool_calls``) fell outside the slice → dropped.
    2. Assistant messages with dangling ``tool_calls`` (no matching tool
       message inside the slice) → the unmatched calls are stripped; if the
       assistant is left with neither calls nor content it is dropped.
    """
    start = 0
    while start < len(messages) and messages[start].role == "tool":
        start += 1
    aligned = list(messages[start:])

    answered: set[str] = set()
    for m in aligned:
        if m.role == "tool" and m.tool_call_id:
            answered.add(m.tool_call_id)

    result: list[Message] = []
    for m in aligned:
        if m.role == "assistant" and m.tool_calls:
            kept = tuple(tc for tc in m.tool_calls if tc.id in answered)
            if len(kept) != len(m.tool_calls):
                if not kept and not (m.content or "").strip():
                    continue  # nothing useful remains of this assistant turn
                m = replace(m, tool_calls=kept)
        result.append(m)
    return result


def _build_summary(messages: tuple[Message, ...]) -> str:
    """Build a structured digest of the message history for compaction.

    Extraction is role-differentiated so the summarizing model sees the
    highest-signal content instead of a uniformly truncated dump:

    - system (first): first 2000 chars (identity / tool list);
    - system (compaction summary): kept in full — it must survive into the
      next incremental merge;
    - user (genuine): kept in full; reminders are dropped;
    - assistant: first 1000 chars + tool-call name list;
    - tool: tool name + success/failure + ref_id + up to 5 scalar fields
      from raw_data + first 300 chars of the payload text.
    """
    parts: list[str] = []
    for msg in messages:
        if msg.role == "user" and msg.source == "reminder":
            continue
        content = msg.content or ""
        if msg.role == "system":
            if _is_summary_message(msg):
                parts.append(f"[SYSTEM] {content}")
            else:
                parts.append(f"[SYSTEM] {_truncate(content, 2000)}")
        elif msg.role == "user":
            parts.append(f"[USER] {content}")
        elif msg.role == "assistant":
            text = _truncate(content, 1000)
            if msg.tool_calls:
                tc_names = [tc.name for tc in msg.tool_calls]
                text += f" [工具调用: {', '.join(tc_names)}]"
            parts.append(f"[ASSISTANT] {text}")
        elif msg.role == "tool":
            parts.append(f"[TOOL] {_summarize_tool_message(msg)}")
        else:
            parts.append(f"[{msg.role.upper()}] {_truncate(content, 500)}")
    return "\n".join(parts)


def _truncate(text: str, limit: int) -> str:
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return text


def _summarize_tool_message(msg: Message) -> str:
    """One-line structured digest of a tool result message."""
    name = msg.name or "tool"
    content = msg.content or ""
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        payload = None
    if not isinstance(payload, dict):
        return f"工具={name} | {_truncate(content, 300)}"

    success = payload.get("success")
    status = "成功" if success else ("失败" if success is not None else "未知")
    bits = [f"工具={name} 状态={status}"]
    data = payload.get("raw_data")
    if isinstance(data, dict):
        ref_id = data.get("ref_id")
        if ref_id and (data.get("__persisted_output__") or data.get("_omitted")):
            bits.append(f"ref_id={ref_id}")
        scalar_items = [
            (k, v)
            for k, v in data.items()
            if isinstance(v, (str, int, float, bool)) and not k.startswith("__")
        ][:5]
        if scalar_items:
            bits.append(" ".join(f"{k}={v!r}" for k, v in scalar_items))
        bits.append(_truncate(str(data), 300))
    else:
        bits.append(_truncate(content, 300))
    return " | ".join(bits)
