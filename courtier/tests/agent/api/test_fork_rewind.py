"""Unit tests for fork/rewind session tree service functions."""

from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

from courtier.agent.api.services.session_service import (
    fork_session_tree,
    rewind_session_tree,
)
from courtier.agent.api.session_store import SessionStore
from courtier.agent.core.conversation_tree import ConversationTree
from courtier.agent.core.state import Message


async def _make_tree_session(
    store: SessionStore, owner: str = "admin"
) -> tuple[str, ConversationTree]:
    session_id = "sess_" + "a" * 12
    tree = ConversationTree.from_messages((Message(role="user", content="hello"),))
    tree_json = json.dumps(tree.serialize(), ensure_ascii=False)
    await store.create(session_id, task="hello", file_id="", owner=owner)
    await store.update(
        session_id,
        tree_json=tree_json,
        current_node_id=tree.root_id,
    )
    return session_id, tree


class TestForkSessionTree:
    @pytest.mark.asyncio
    async def test_fork_current_node(self, tmp_path):
        store = SessionStore(str(tmp_path))
        session_id, tree = await _make_tree_session(store)
        result = await fork_session_tree(
            store, "admin", True, session_id, None, "explore"
        )
        assert result["new_node_id"] != tree.root_id
        assert len(result["messages"]) == 1
        updated = await store.get(session_id)
        assert updated.current_node_id == result["new_node_id"]

    @pytest.mark.asyncio
    async def test_fork_specific_node(self, tmp_path):
        store = SessionStore(str(tmp_path))
        session_id, tree = await _make_tree_session(store)
        result = await fork_session_tree(
            store, "admin", True, session_id, tree.root_id, "explore"
        )
        assert result["new_node_id"] != tree.root_id

    @pytest.mark.asyncio
    async def test_fork_unknown_node(self, tmp_path):
        store = SessionStore(str(tmp_path))
        session_id, _tree = await _make_tree_session(store)
        with pytest.raises(HTTPException) as exc_info:
            await fork_session_tree(store, "admin", True, session_id, "unknown", "")
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_fork_session_without_tree(self, tmp_path):
        store = SessionStore(str(tmp_path))
        session_id = "sess_" + "b" * 12
        await store.create(session_id, task="hello", file_id="", owner="admin")
        with pytest.raises(HTTPException) as exc_info:
            await fork_session_tree(store, "admin", True, session_id, None, "")
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_fork_ownership_enforced(self, tmp_path):
        store = SessionStore(str(tmp_path))
        session_id, _tree = await _make_tree_session(store, owner="admin")
        with pytest.raises(HTTPException) as exc_info:
            await fork_session_tree(store, "user1", False, session_id, None, "")
        assert exc_info.value.status_code == 404


class TestRewindSessionTree:
    @pytest.mark.asyncio
    async def test_rewind_existing_node(self, tmp_path):
        store = SessionStore(str(tmp_path))
        session_id, tree = await _make_tree_session(store)
        result = await rewind_session_tree(
            store, "admin", True, session_id, tree.root_id
        )
        assert result["current_node_id"] == tree.root_id
        assert len(result["messages"]) == 1

    @pytest.mark.asyncio
    async def test_rewind_unknown_node(self, tmp_path):
        store = SessionStore(str(tmp_path))
        session_id, _tree = await _make_tree_session(store)
        with pytest.raises(HTTPException) as exc_info:
            await rewind_session_tree(store, "admin", True, session_id, "unknown")
        assert exc_info.value.status_code == 404
