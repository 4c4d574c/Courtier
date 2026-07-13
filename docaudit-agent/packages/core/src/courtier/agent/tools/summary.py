"""ToolSummary — structured display data extracted from ToolResult."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class ToolSummary:
    """Structured summary of a tool result for TUI rendering.

    status: "ok" | "warn" | "err" — maps to card left-border color
    chips: semantic key-value pairs for display (e.g. [("页数", "12")])
    issue_counts: present for error/warning tools (e.g. {"err": 3, "warn": 2, "ok": 10})
    detail: full result data for expandable detail view (None = no expand)
    """
    status: Literal["ok", "warn", "err"]
    chips: list[tuple[str, str]]
    issue_counts: dict[str, int] | None
    detail: Any | None


import logging

logger = logging.getLogger(__name__)


def summarize_result(result: Any) -> ToolSummary:
    """Produce a ToolSummary from a ToolResult using type-based reflection.

    Determines status and chips by introspecting the ToolResult's data type
    and metadata.  Dict keys become chips (only scalar values), lists produce
    an item count chip, and strings/nulls generate no chips.  Status is derived
    from ``metadata.issue_counts`` when present.
    """
    # import here to avoid circular import with tools.protocol
    from .protocol import ToolResult

    if not isinstance(result, ToolResult):
        logger.warning(
            "summarize_result received %s instead of ToolResult, returning err",
            type(result).__name__,
        )
        return ToolSummary(
            status="err",
            chips=[("error", f"非ToolResult: {type(result).__name__}")],
            issue_counts=None,
            detail=None,
        )

    if not result.success:
        return ToolSummary(
            status="err",
            chips=[("error", result.error or "未知错误")],
            issue_counts=None,
            detail=str(result.error),
        )

    data = result.data
    issue_counts = result.metadata.get("issue_counts")

    # Determine status from issue counts
    status: Literal["ok", "warn", "err"] = "ok"
    if isinstance(issue_counts, dict):
        errs = issue_counts.get("err", 0)
        warns = issue_counts.get("warn", 0)
        if errs > 0:
            status = "err"
        elif warns > 0:
            status = "warn"

    # Build chips from data
    chips: list[tuple[str, str]] = []
    if isinstance(data, dict):
        # Handle __persisted_output__ marker: show only meaningful metadata
        if data.get("__persisted_output__"):
            size = data.get("size_chars")
            if isinstance(size, int):
                chips.append(("大小", f"{size} 字符"))
            ct = data.get("content_type")
            if isinstance(ct, str):
                chips.append(("类型", ct))
        else:
            for key, val in data.items():
                # Skip internal marker / metadata keys (Python dunder convention).
                if key.startswith("__"):
                    continue
                if isinstance(val, (str, int, float)):
                    # Truncate long string values to keep cards readable
                    val_str = str(val)
                    if len(val_str) > 80:
                        val_str = val_str[:80] + "…"
                    chips.append((key, val_str))
    elif isinstance(data, list):
        chips.append(("items", str(len(data))))
    elif isinstance(data, str):
        pass  # strings don't auto-generate chips

    return ToolSummary(
        status=status,
        chips=chips,
        issue_counts=issue_counts if isinstance(issue_counts, dict) else None,
        detail=data,
    )
