"""ContextManager — three-layer context budget control for the agent loop.

Layer 1: Large tool outputs → persisted to disk, preview in context.
Layer 2: Old tool results → replaced with placeholders (keep last N).
Layer 3: Full compaction → LLM summarizes entire history when budget exceeded.

Learning from: https://learn.shareai.run/zh/s06/
"""

from __future__ import annotations

import json
import logging
import unicodedata
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .cache_store import CacheStore
from .state import Message

if TYPE_CHECKING:
    from .model import ModelClient

logger = logging.getLogger(__name__)

MAX_CONTEXT_CHARS = 40_000  # trigger full compaction when exceeded
LARGE_OUTPUT_THRESHOLD = 3_000  # persist to disk when tool result exceeds this
RECENT_TOOL_RESULTS = 5  # keep last N tool results intact
MAX_CONTEXT_CHARS_AFTER_COMPACT = 25_000  # target after compaction


@dataclass
class CompactState:
    """Tracks compaction status across the agent loop."""

    has_compacted: bool = False
    last_summary: str | None = None
    compact_count: int = 0


class ContextManager:
    """Three-layer context budget management.

    Usage:
        mgr = ContextManager(model=model_client, cache_dir=".agent_cache")
        # Layer 1: wrap tool result
        safe_data = mgr.persist_large_output(tool_name, result.data)
        # Layer 2: after adding observation
        messages = mgr.micro_compact(messages)
        # Layer 3: before next think
        messages = await mgr.compact_if_needed(messages)
    """

    def __init__(
        self,
        model: ModelClient | None = None,
        cache_dir: str = ".agent_cache",
        max_context_chars: int = MAX_CONTEXT_CHARS,
        large_output_threshold: int = LARGE_OUTPUT_THRESHOLD,
        recent_tool_results: int = RECENT_TOOL_RESULTS,
        cache_store: CacheStore | None = None,
        artifact_store: Any | None = None,
    ) -> None:
        self._model = model
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self.max_context_chars = max_context_chars
        self.large_output_threshold = large_output_threshold
        self.recent_tool_results = recent_tool_results
        self.state = CompactState()
        # Unified store: prefer ArtifactStore (which now subsumes CacheStore
        # functionality) over legacy CacheStore.  The duck-typing works because
        # ArtifactStore provides persist, resolve_refs, ref_map, and set_ref.
        store = artifact_store or cache_store
        self._cache = store or CacheStore(
            cache_dir=str(self._cache_dir),
            large_output_threshold=large_output_threshold,
        )

    # -- Layer 1: Persist large outputs ---------------------------------------

    async def persist_large_output(
        self, tool_name: str, data: object, force: bool = False
    ) -> object:
        """If data is large, save to disk and return a preview marker with ref_id.

        When *force* is True, always persists even when the serialized data is
        below ``large_output_threshold``, guaranteeing a ref exists for the LLM
        to reference downstream.

        Returns the original data if small (and not forced), or a
        persisted-output dict with ref_id.
        """
        if data is None:
            return data

        result = await self._cache.persist(data, tool_name, force=force)
        return result.data

    # -- Force persist ---------------------------------------------------------

    async def force_persist(self, tool_name: str, data: object) -> str | None:
        """Always persist *data* to disk and return a ref_id, regardless of size.

        Unlike :meth:`persist_large_output`, this always writes to disk even when
        the serialized data is below ``large_output_threshold``.  This guarantees
        a ref exists for the LLM to reference downstream.

        Returns the generated ref_id (e.g. ``"$ref:parse_document:1"``) or
        ``None`` if the data cannot be serialised.
        """
        if data is None:
            return None

        result = await self._cache.persist(data, tool_name, force=True)
        if result.persisted:
            logger.debug(
                "force_persist: %s -> %s",
                tool_name,
                result.ref_id,
            )
            return result.ref_id
        return None

    # -- Ref resolution -------------------------------------------------------

    def resolve_refs(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        """Recursively resolve $ref:tool:N references in tool call arguments.

        Returns a new dict with ref strings replaced by data loaded from disk.
        Unknown refs and missing files are kept as-is with a warning log.
        """
        return self._cache.resolve_refs(kwargs)

    # -- Layer 2: Micro-compact old tool results ------------------------------

    async def micro_compact(self, messages: tuple[Message, ...]) -> tuple[Message, ...]:
        """Replace old tool-result messages with placeholders.

        Keeps the last N tool results intact; older ones become omission markers
        with a ref_id for recovery. If the result was already persisted by Layer 1,
        the existing ref_id is reused. Otherwise, the result is persisted to disk
        first so it can be recovered if needed.

        System and user messages are always preserved.
        """
        tool_indices = [i for i, m in enumerate(messages) if m.role == "tool"]

        if len(tool_indices) <= self.recent_tool_results:
            return messages

        to_compact = set(tool_indices[: -self.recent_tool_results])

        compacted = []
        for i, msg in enumerate(messages):
            if i in to_compact:
                tool_name = msg.name or f"tool_call_{msg.tool_call_id}"

                ref_id = self._extract_ref_id(msg)
                if ref_id is None:
                    ref_id = await self._persist_tool_message(msg, tool_name)

                note = (
                    "Result omitted by micro-compact (old tool result). "
                    "Use $ref id as tool argument to recover full data."
                )
                placeholder: dict[str, Any] = {
                    "_omitted": True,
                    "tool": tool_name,
                    "note": note,
                }
                if ref_id:
                    placeholder["ref_id"] = ref_id

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

        # Deduplicate reminder messages: PRE_TURN_REMINDER and PERIODIC_REMINDER
        # are injected by the loop every turn / every 5 turns.  Micro-compact
        # normally only operates on tool messages, so these reminders would
        # accumulate indefinitely.  Keep only the last occurrence of each.
        compacted = _deduplicate_reminders(compacted)

        return tuple(compacted)

    def _extract_ref_id(self, msg: Message) -> str | None:
        """Extract ref_id from a tool message that was already persisted by Layer 1.

        Returns None if the message content is not a persisted-output marker.
        """
        if not msg.content:
            return None
        try:
            payload = json.loads(msg.content)
            data = payload.get("data", {})
            if isinstance(data, dict) and data.get("__persisted_output__") is True:
                return data.get("ref_id")
        except (json.JSONDecodeError, TypeError):
            pass
        return None

    async def _persist_tool_message(self, msg: Message, tool_name: str) -> str | None:
        """Persist a non-persisted tool message to disk and return a new ref_id.

        Extracts the inner data from the tool result envelope
        ({"success": ..., "data": ..., "error": ...}) and persists it.
        Returns None if the message has no data to persist.
        """
        if not msg.content:
            return None
        try:
            payload = json.loads(msg.content)
            data = payload.get("data")
            if data is None:
                return None
        except (json.JSONDecodeError, TypeError):
            return None

        result = await self._cache.persist(data, tool_name, force=True)
        if not result.persisted:
            return None  # OSError or empty ref_id — caller checks if ref_id is None

        logger.debug(
            "Micro-compact persisted: %s -> %s",
            tool_name,
            result.ref_id,
        )
        return result.ref_id

    # -- Layer 3: Full compaction ---------------------------------------------

    async def compact_if_needed(
        self, messages: tuple[Message, ...]
    ) -> tuple[Message, ...]:
        """If context exceeds budget, summarize the conversation history.

        Returns a compacted message tuple with a summary replacing old messages.
        """
        if self._estimate_chars(messages) < self.max_context_chars:
            return messages

        logger.info(
            "Context budget exceeded (%d chars), triggering full compaction",
            self._estimate_chars(messages),
        )
        return await self._full_compact(messages)

    async def force_compact(self, messages: tuple[Message, ...]) -> tuple[Message, ...]:
        """Force a full compaction regardless of budget (e.g., /compact command)."""
        logger.info("Forced compaction requested")
        return await self._full_compact(messages)

    async def _full_compact(self, messages: tuple[Message, ...]) -> tuple[Message, ...]:
        """Summarize history into a single compacted message."""
        # Guard: if already compacted to minimum form (system prompt +
        # compaction note + last user message ≈ 3 messages), the context
        # budget is dominated by the system prompt itself and further
        # compaction cannot help.  Skip to avoid an infinite re-compaction
        # loop.
        if self.state.has_compacted and len(messages) <= 3:
            logger.warning(
                "Already compacted to minimum (%d messages), " "skipping re-compaction",
                len(messages),
            )
            return messages

        if self._model is None:
            logger.warning("Cannot compact: no model configured")
            return messages

        # Build a summarization prompt
        summary_text = _build_summary(messages)
        compact_prompt = (
            "你是一个上下文压缩助手。请将以下对话历史压缩为一个紧凑的摘要，"
            "必须保留以下五类信息：\n"
            "1. 当前任务目标\n"
            "2. 已完成的关键操作\n"
            "3. 涉及的文件\n"
            "4. 关键决策和约束\n"
            "5. 下一步具体行动\n\n"
            "输入对话历史：\n"
        )
        compact_messages: list[dict] = [
            {"role": "system", "content": compact_prompt + summary_text},
            {"role": "user", "content": "请压缩以上对话历史，只输出摘要。"},
        ]

        try:
            response = await self._model.generate(compact_messages)
            summary = response.content or summary_text[:500]
        except Exception as exc:
            logger.warning("Compaction summary failed: %s, using fallback", exc)
            # Keep the most recent messages instead of truncating to 800 chars,
            # which would lose almost all context.  8 recent messages preserve
            # enough conversational context to recover gracefully.
            keep_recent = min(8, len(messages))
            fallback_compacted = list(messages[-keep_recent:])
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
            logger.info(
                "Compaction fallback: %d → %d messages",
                len(messages),
                len(fallback_compacted),
            )
            return tuple(fallback_compacted)

        self.state.has_compacted = True
        self.state.last_summary = summary
        self.state.compact_count += 1

        # Build compacted messages:
        # 1. Original system prompt (identity, tools, behavioral rules) — must
        #    be preserved or the agent forgets who it is and what it can do.
        # 2. Compaction summary note.
        # 3. Most recent user message for continuity.
        compacted: list[Message] = []

        # Preserve the original system prompt when it exists.
        if messages and messages[0].role == "system":
            compacted.append(messages[0])

        compacted.append(
            Message(
                role="system",
                content=(
                    f"[上下文压缩 #{self.state.compact_count}] "
                    f"以下为之前对话的摘要，请继续完成任务：\n\n{summary}"
                ),
            ),
        )

        # Keep the most recent user message to maintain continuity
        last_user = None
        for m in reversed(messages):
            if m.role == "user" and m.content:
                last_user = m
                break
        if last_user:
            compacted.append(last_user)

        logger.info(
            "Compaction complete: %d → %d messages, summary %d chars",
            len(messages),
            len(compacted),
            len(summary),
        )
        return tuple(compacted)

    # -- Helpers --------------------------------------------------------------

    def _estimate_chars(self, messages: tuple[Message, ...]) -> int:
        """Estimate total character count of all messages."""
        total = 0
        for msg in messages:
            if msg.content:
                total += len(msg.content)
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    total += len(json.dumps(tc.arguments, ensure_ascii=False))
        return total

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

    def get_cache_store(self) -> dict[str, str]:
        """Return the cached output ref_id -> filepath mapping."""
        return dict(self._cache.ref_map)

    def get_ref_instructions(self) -> str:
        """Return a short instruction block for the LLM about how to use ref IDs."""
        return (
            "工具结果可能保存为 $ref 缓存引用。构造下游工具参数时，"
            "优先直接把业务 $ref 传给目标工具，系统会自动加载或适配参数。\n"
            "使用 list_artifacts 查看可用的类型化工件及其元数据，"
            "使用 get_artifact 按需获取投影后的数据。\n"
            "当工具返回 __persisted_output__ 标记时，预览通常已足够理解结果。"
            "旧工具结果可能被微压缩为 _omitted 占位符。"
            "普通业务流程不应依赖恢复这些调试内容。"
        )


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


def _build_summary(messages: tuple[Message, ...]) -> str:
    """Build a text summary of the message history for compaction.

    System messages (identity, tools, rules) get a larger budget since
    they carry critical structural information.  All other messages are
    truncated more aggressively.
    """
    parts: list[str] = []
    for msg in messages:
        role = msg.role.upper()
        content = msg.content or ""
        limit = 2000 if msg.role == "system" else 500
        if len(content) > limit:
            content = content[: limit - 3] + "..."
        if msg.tool_calls:
            tc_names = [tc.name for tc in msg.tool_calls]
            content += f" [工具调用: {', '.join(tc_names)}]"
        parts.append(f"[{role}] {content}")
    return "\n".join(parts)
