"""Session service — simple CRUD wrappers around SessionStore."""

from __future__ import annotations

import json
import logging
from typing import Any, cast

from fastapi import HTTPException

from ...core.conversation_tree import ConversationTree

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
