"""删除会话时连带清除 .agent_sessions/<id> 工作区（路径策略的会话根）。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from courtier.agent.api.services.session_service import delete_session
from courtier.agent.api.session_store import SessionStore

SESSION_ID = "sess_7e57e57e0001"


def _make_store(tmp_path: Path) -> SessionStore:
    return SessionStore(str(tmp_path / "store"))


def test_delete_removes_session_workspace(tmp_path):
    async def _run():
        store = _make_store(tmp_path)
        await store.create(SESSION_ID, "task", "")
        # cache_dir 形如 uploads/.cache，根 = parent/.agent_sessions（与
        # agent_service 的 session_workspace 计算一致）
        workspace = tmp_path / ".agent_sessions" / SESSION_ID
        (workspace / "sub").mkdir(parents=True)
        (workspace / "greeting.txt").write_text("hi", encoding="utf-8")
        deleted = await delete_session(
            store, SESSION_ID, "admin", is_admin=True, cache_dir=str(tmp_path / ".cache")
        )
        return deleted, workspace

    deleted, workspace = asyncio.run(_run())
    assert deleted is True
    assert not workspace.exists()


def test_delete_tolerates_missing_workspace(tmp_path):
    async def _run():
        store = _make_store(tmp_path)
        await store.create(SESSION_ID, "task", "")
        # .agent_sessions 根都不存在——删除照常成功
        return await delete_session(
            store, SESSION_ID, "admin", is_admin=True, cache_dir=str(tmp_path / ".cache")
        )

    assert asyncio.run(_run()) is True


def test_traversalish_id_skips_workspace_removal(tmp_path):
    """非规范会话 id（防路径穿越）不触碰文件系统，但会话照常删除。"""
    async def _run():
        store = _make_store(tmp_path)
        await store.create(SESSION_ID, "task", "")
        sentinel = tmp_path / ".agent_sessions"
        sentinel.mkdir(parents=True)
        (sentinel / "evil").write_text("x", encoding="utf-8")
        # 直接注入非法 id 到 store 以模拟绕过（delete_session 走 get_owned，这里只验证
        # _remove_session_workspace 的正则防线）
        from courtier.agent.api.services.session_service import _remove_session_workspace

        await _remove_session_workspace(str(tmp_path / ".cache"), "../../etc")
        return sentinel

    sentinel = asyncio.run(_run())
    assert (sentinel / "evil").exists()
