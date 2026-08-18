"""Session service — simple CRUD wrappers around SessionStore."""

from __future__ import annotations

import json
import logging
from typing import Any, cast

from fastapi import HTTPException

from ...core.conversation_tree import ConversationTree
from ...core.state import Message
from ..models import SessionRecord
from .stream_service import deserialize_messages, serialize_messages

logger = logging.getLogger(__name__)


async def list_sessions(
    store: Any,
    current_user: str,
    is_admin: bool = False,
    skip: int = 0,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """List sessions visible to the current user, most recent first."""
    return cast(
        list[dict[str, Any]],
        await store.list_all(current_user=current_user, is_admin=is_admin, skip=skip, limit=limit),
    )


async def get_session(
    store: Any, session_id: str, current_user: str, is_admin: bool = False
) -> dict[str, Any] | None:
    """Get full detail for a session, or None if not found or not owned."""
    session = await store.get_owned(session_id, current_user, is_admin)
    if session is None:
        return None
    return cast(dict[str, Any], session.to_detail_dict())


async def delete_session(
    store: Any,
    session_id: str,
    current_user: str,
    is_admin: bool = False,
    cache_dir: str = "",
) -> bool:
    """Delete a session if the current user is allowed to access it.

    When the deleted session held artifact refs, cache files that no other
    session references are garbage-collected (best-effort).
    """
    session = await store.get_owned(session_id, current_user, is_admin)
    if session is None:
        return False
    snapshot = session.artifact_snapshot
    deleted = cast(bool, await store.delete(session_id))
    if deleted and snapshot and cache_dir:
        gc = getattr(store, "gc_orphan_cache_files", None)
        if gc is not None:
            try:
                await gc(snapshot, cache_dir)
            except Exception:
                logger.warning(
                    "Cache GC failed after deleting session %s",
                    session_id,
                    exc_info=True,
                )
    return deleted


async def fork_session_tree(
    store: Any,
    current_user: str,
    is_admin: bool,
    session_id: str,
    node_id: str | None,
    reason: str,
) -> dict[str, Any]:
    """Fork a conversation tree node and persist the new branch."""
    session = await store.get_owned(session_id, current_user, is_admin)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if not session.tree_json:
        raise HTTPException(status_code=404, detail="Session has no conversation tree")

    try:
        tree = ConversationTree.from_serialized(json.loads(session.tree_json))
    except Exception as exc:
        logger.exception("Failed to deserialize tree for session %s", session_id)
        raise HTTPException(status_code=500, detail=f"Invalid conversation tree: {exc}") from exc

    target_node_id = node_id or session.current_node_id or tree.root_id
    if target_node_id is None or tree.get(target_node_id) is None:
        raise HTTPException(status_code=404, detail="Node not found")

    child = tree.fork(target_node_id, reason=reason)
    await store.update(
        session_id,
        tree_json=json.dumps(tree.serialize(), ensure_ascii=False),
        current_node_id=child.node_id,
    )
    return {
        "new_node_id": child.node_id,
        "messages": [m.to_openai_dict() for m in child.messages],
    }


async def rewind_session_tree(
    store: Any,
    current_user: str,
    is_admin: bool,
    session_id: str,
    node_id: str,
) -> dict[str, Any]:
    """Rewind a conversation tree to an existing node and persist it."""
    session = await store.get_owned(session_id, current_user, is_admin)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if not session.tree_json:
        raise HTTPException(status_code=404, detail="Session has no conversation tree")

    try:
        tree = ConversationTree.from_serialized(json.loads(session.tree_json))
    except Exception as exc:
        logger.exception("Failed to deserialize tree for session %s", session_id)
        raise HTTPException(status_code=500, detail=f"Invalid conversation tree: {exc}") from exc

    if tree.get(node_id) is None:
        raise HTTPException(status_code=404, detail="Node not found")

    node = tree.rewind(node_id)
    await store.update(
        session_id,
        tree_json=json.dumps(tree.serialize(), ensure_ascii=False),
        current_node_id=node.node_id,
    )
    return {
        "current_node_id": node.node_id,
        "messages": [m.to_openai_dict() for m in node.messages],
    }


# -- Edit-resend: truncate a session back to an earlier turn --------------------
#
# Editing the user message of turn N physically removes turn N and everything
# after it from the persisted record, then the edited text starts a fresh
# turn N through the normal continuation flow.


def _is_real_user_message(msg: Message) -> bool:
    """True only for user-typed turn messages.

    Loop-injected user-role messages (reminder/inline/hint) carry a ``source``
    tag; genuine turn messages never do.
    """
    return msg.role == "user" and msg.source is None


def _turn_cut_index(messages: tuple[Message, ...], turn_index: int) -> int | None:
    """Return the array index where turn *turn_index* begins (its user message)."""
    seen = -1
    for i, msg in enumerate(messages):
        if _is_real_user_message(msg):
            seen += 1
            if seen == turn_index:
                return i
    return None


def session_has_compacted(context_state: str) -> bool:
    """True when the persisted CompactState records a past compaction."""
    if not context_state:
        return False
    try:
        state = json.loads(context_state)
    except json.JSONDecodeError:
        return False
    return bool(state.get("has_compacted") or state.get("compact_count"))


def _prune_tree(
    tree_json: str,
    current_node_id: str | None,
    truncated_messages: tuple[Message, ...],
) -> dict[str, Any]:
    """Drop tree nodes past the truncation point; move the active pointer.

    Node messages are full-conversation prefixes recorded per loop step, so
    without compaction (a precondition for editing) a node belongs to a
    revoked turn iff its snapshot is longer than the truncated history.
    The root always survives — it is the continuation base — and is trimmed
    to the truncated prefix when longer.  Best-effort: the tree is the
    secondary format; ``messages_json`` remains the context source of truth.
    """
    tree = ConversationTree.from_serialized(json.loads(tree_json))
    if not tree.nodes or tree.root_id is None:
        return {}
    parents = {nid: n.parent_id for nid, n in tree.nodes.items()}
    kept_len = len(truncated_messages)
    root = tree.nodes[tree.root_id]
    if len(root.messages) > kept_len:
        root.messages = truncated_messages
    kept = {nid for nid, n in tree.nodes.items() if len(n.messages) <= kept_len}
    kept.add(tree.root_id)
    for nid in list(tree.nodes):
        if nid not in kept:
            del tree.nodes[nid]
    for node in tree.nodes.values():
        node.children = [c for c in node.children if c in kept]

    # Walk up from the old pointer to its deepest surviving ancestor.
    new_current = current_node_id
    visited: set[str] = set()
    while new_current is not None and new_current not in kept and new_current not in visited:
        visited.add(new_current)
        new_current = parents.get(new_current)
    if new_current is None or new_current not in kept:
        new_current = max(
            kept, key=lambda nid: (len(tree.nodes[nid].messages), nid == tree.root_id)
        )
    return {
        "tree_json": json.dumps(tree.serialize(), ensure_ascii=False),
        "current_node_id": new_current,
    }


def compute_turn_truncation(session: SessionRecord, turn_index: int) -> dict[str, Any]:
    """Compute the SessionStore.update kwargs that remove turn *turn_index*
    and everything after it.  Pure: performs no store I/O.

    Raises ValueError when *turn_index* is out of range or the persisted
    message history does not contain that many genuine user messages.
    """
    num_turns = len(session.turn_messages)
    if turn_index < 0 or turn_index >= num_turns:
        raise ValueError(f"轮次越界: editTurn={turn_index}（共 {num_turns} 轮）")

    kwargs: dict[str, Any] = {}

    # LLM context: cut right before the turn's genuine user message.  A user
    # message never sits mid tool-call sequence, but a stopped/errored turn
    # can leave a dangling assistant tool_calls tail — realign defensively.
    truncated_messages: tuple[Message, ...] = ()
    if session.messages_json:
        from ...core.context_manager import _align_tool_boundaries

        messages = deserialize_messages(session.messages_json)
        cut = _turn_cut_index(messages, turn_index)
        if cut is None:
            raise ValueError("会话上下文与轮次记录不一致，无法按轮撤销")
        truncated_messages = tuple(_align_tool_boundaries(list(messages[:cut])))
        kwargs["messages_json"] = serialize_messages(truncated_messages)

    # Turn bookkeeping.
    kwargs["turn_messages"] = session.turn_messages[:turn_index]
    kwargs["turn_step_starts"] = session.turn_step_starts[:turn_index]
    kwargs["turn_conclusions"] = session.turn_conclusions[:turn_index]

    # Steps / thoughts from revoked turns.
    if turn_index < len(session.turn_step_starts):
        step_cut = session.turn_step_starts[turn_index]
    else:
        step_cut = len(session.steps)
    kwargs["steps"] = session.steps[:step_cut]
    kwargs["thoughts"] = [t for t in session.thoughts if t.turn_index < turn_index]

    # Session-level conclusion falls back to the last surviving turn's so the
    # legacy last-turn fallback in _build_turns keeps rendering it.
    conclusions = kwargs["turn_conclusions"]
    kwargs["conclusion"] = conclusions[-1] if conclusions else ""

    # The revoked turns may be the ones that errored/were stopped; the
    # surviving history's natural resting state is completed.
    if session.status in ("error", "stopped"):
        kwargs["status"] = "completed"
        kwargs["error_detail"] = None

    # Compaction state: compacted sessions are rejected before this point, so
    # no compaction has happened and a fresh state is exact (same as a new
    # session, which stores "").
    kwargs["context_state"] = ""

    # Conversation tree (secondary format).
    if session.tree_json and session.messages_json:
        try:
            kwargs.update(
                _prune_tree(session.tree_json, session.current_node_id, truncated_messages)
            )
        except Exception:
            logger.warning(
                "Failed to prune conversation tree for session %s; leaving tree as-is",
                session.id,
                exc_info=True,
            )

    return kwargs


async def truncate_session_to_turn(
    store: Any,
    current_user: str,
    is_admin: bool,
    session_id: str,
    turn_index: int,
) -> SessionRecord:
    """Physically remove turn *turn_index* and everything after it.

    Validates ownership, run state, compaction history, and bounds before
    truncating.  The caller (SSE continuation flow) re-registers the edited
    turn afterwards via ``add_turn``.
    """
    session = await store.get_owned(session_id, current_user, is_admin)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.status == "running":
        raise HTTPException(status_code=409, detail="会话正在运行，请先停止再编辑")
    if session_has_compacted(session.context_state):
        raise HTTPException(status_code=409, detail="该会话历史已压缩，不支持编辑重发")
    try:
        kwargs = compute_turn_truncation(session, turn_index)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    updated = await store.update(session_id, **kwargs)
    if updated is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return updated
