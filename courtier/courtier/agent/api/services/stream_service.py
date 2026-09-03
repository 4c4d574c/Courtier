"""Stream service — session-state serialization helpers.

SSE streaming itself now lives in ``run_manager`` (``stream_run`` /
``generate_sse_stream``): runs are connection-independent background tasks
and SSE connections are pure readers of the run event log.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


# -- Serialization helpers for multi-turn state ---------------------------------


def serialize_messages(messages: tuple) -> str:
    """Serialize AgentState messages to a JSON string for persistence.

    Part-list content (multimodal user turns) persists in its internal
    logical form — media parts are file references, never bytes.
    """
    from ...core.content_parts import parts_to_internal

    data = []
    for msg in messages:
        d: dict = {"role": msg.role, "content": parts_to_internal(msg.content)}
        if msg.tool_calls:
            d["tool_calls"] = [
                {"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in msg.tool_calls
            ]
        if msg.tool_call_id:
            d["tool_call_id"] = msg.tool_call_id
        if msg.name:
            d["name"] = msg.name
        if msg.source:
            d["source"] = msg.source
        data.append(d)
    return json.dumps(data, ensure_ascii=False)


def deserialize_messages(json_str: str) -> tuple:
    """Deserialize a JSON string back to a tuple of Message objects."""
    from ...core.content_parts import parse_content
    from ...core.model import ToolCall
    from ...core.state import Message as _Msg

    raw = json.loads(json_str)
    messages = []
    for d in raw:
        tool_calls = None
        if d.get("tool_calls"):
            tool_calls = []
            for tc in d["tool_calls"]:
                try:
                    tool_calls.append(ToolCall(**tc))
                except (TypeError, ValueError) as exc:
                    logger.warning("Skipping malformed tool call in deserialized messages: %s", exc)
        messages.append(
            _Msg(
                role=d["role"],
                content=parse_content(d.get("content")),
                tool_calls=tuple(tool_calls) if tool_calls is not None else None,
                tool_call_id=d.get("tool_call_id"),
                name=d.get("name"),
                source=d.get("source"),
            )
        )
    return tuple(messages)


def reconstruct_state(
    messages_json: str,
    tree_json: str = "",
    current_node_id: str | None = None,
) -> Any:
    """Reconstruct an AgentState from serialised messages and optional tree."""
    from ...core.conversation_tree import ConversationTree
    from ...core.state import AgentState

    if not messages_json:
        return None
    messages = deserialize_messages(messages_json)
    if not messages:
        return None
    tree = None
    if tree_json:
        try:
            tree = ConversationTree.from_serialized(json.loads(tree_json))
        except Exception:
            logger.exception("Failed to deserialize conversation tree")
    return AgentState(
        status="completed",
        messages=messages,
        current_step=0,
        max_steps=20,
        tree=tree,
        current_node_id=current_node_id or (tree.root_id if tree else None),
    )
