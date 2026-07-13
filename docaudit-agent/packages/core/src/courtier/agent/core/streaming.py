"""Streaming helpers — pure functions used by model.py.

The delta-parsing logic here is intentionally shared so that the
main-process ModelClient uses a single tool-call-buffering algorithm.
"""

from __future__ import annotations

from typing import Any


def extract_stream_delta(chunk: Any) -> Any | None:
    """Extract the delta object from one SSE chunk.

    Returns None when the chunk carries no delta (usage-only frames, etc.),
    so callers can ``continue`` early.
    """
    if not chunk.choices:
        return None
    return chunk.choices[0].delta


def extract_reasoning(delta: Any) -> str:
    """Return the reasoning/thinking text from a delta, or ''."""
    return (
        getattr(delta, "reasoning_content", None)
        or getattr(delta, "reasoning", None)
        or ""
    )


def buffer_tool_call_delta(
    delta: Any,
    buffers: dict[int, dict[str, str]],
    *,
    name_key: str = "name",
    arguments_key: str = "arguments",
) -> None:
    """Append streaming tool-call fragments in *delta* into *buffers*.

    Each buffer entry is ``{"id": str, <name_key>: str, <arguments_key>: str}``.
    Callers may use different key names (e.g. model.py uses ``arguments_str``;
    the Plugin SDK uses ``arguments``) via the keyword arguments.
    """
    for tc_delta in delta.tool_calls or []:
        idx = tc_delta.index
        if idx not in buffers:
            buffers[idx] = {"id": "", name_key: "", arguments_key: ""}
        buf = buffers[idx]
        if tc_delta.id:
            buf["id"] = tc_delta.id
        if tc_delta.function:
            if tc_delta.function.name:
                buf[name_key] += tc_delta.function.name
            if tc_delta.function.arguments:
                buf[arguments_key] += tc_delta.function.arguments
