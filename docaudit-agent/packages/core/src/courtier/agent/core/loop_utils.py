"""Pure utility functions for the agent loop — summaries, formatting, similarity."""

from __future__ import annotations

import difflib
import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from courtier.agent.core.execution_result import ExecutionResult


def tool_result_summary(result: ExecutionResult) -> str:
    """Produce a short summary string for a tool/sub-agent result.

    Handles scalar, dict, list, str, and nested/dataframe-like types.
    For dicts, counts top-level keys and distinguishes persisted refs.
    Non-scalar dict values (nested dicts) are counted in sub-field totals.
    """
    if not result.success:
        return f"失败: {result.error or '未知错误'}"
    data = result.raw_data
    if data is None:
        if result.result_id:
            return f"完成 (已持久化: {result.result_id})"
        return "完成 (无返回数据)"
    if isinstance(data, dict):
        if data.get("__persisted_output__"):
            size = data.get("size_chars", 0)
            return f"完成 ({fmt_size(size)}, 已缓存)"
        scalar = sum(1 for v in data.values() if isinstance(v, (str, int, float)))
        nested = sum(1 for v in data.values() if isinstance(v, dict))
        list_vals = sum(1 for v in data.values() if isinstance(v, list))
        if nested or list_vals:
            parts: list[str] = [f"完成 ({len(data)} 个字段"]
            if scalar:
                parts.append(f", {scalar} 标量")
            if nested:
                parts.append(f", {nested} 嵌套对象")
            if list_vals:
                parts.append(f", {list_vals} 列表")
            parts.append(")")
            return "".join(parts)
        return f"完成 ({len(data)} 个字段)"
    if isinstance(data, list):
        return f"完成 ({len(data)} 项)"
    if isinstance(data, str):
        preview = data[:50].replace("\n", " ")
        if len(data) > 50:
            preview += "..."
        return preview
    if isinstance(data, (int, float, bool)):
        return f"完成 ({data})"
    return "完成"


def fmt_size(chars: int) -> str:
    """Format byte/char count into human-readable string."""
    if chars >= 10_000:
        return f"{chars // 1000}k 字符"
    if chars >= 1_000:
        return f"{chars / 1000:.1f}k 字符"
    return f"{chars} 字符"


def similarity(a: str, b: str) -> float:
    """Compute string similarity ratio using difflib (0.0–1.0)."""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def normalize_args_for_dedup(args: dict) -> str:
    """Return a canonical, normalized JSON key for duplicate-call detection.

    Strips values that are known to vary without semantic change:
    - ``label`` (display-only metadata)
    - ``_timestamp`` / ``_request_id`` (transient ids)
    """
    stripped = {k: v for k, v in args.items() if k not in ("label", "_timestamp", "_request_id")}
    return json.dumps(stripped, sort_keys=True, ensure_ascii=False)
