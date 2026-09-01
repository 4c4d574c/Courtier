"""MemoryManager suite (file-based memory + recall injection).

Memory is files under ``<cache_dir>/../.agent_memory`` (common/ +
per-domain dirs, MEMORY.md indexes); the manager's own logic under test
here is the recall injection and fork config propagation.  Working-tier
(compaction) coverage lives in test_context_manager.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from courtier.agent.core.memory_manager import MemoryManager
from courtier.agent.core.state import Message

from .conftest import PRE_TURN_REMINDER


@pytest.fixture
def manager(tmp_path):
    return MemoryManager(cache_dir=str(tmp_path / "cache"), session_id="sess_1")


@pytest.fixture
def memory_home(tmp_path):
    home = tmp_path / ".agent_memory"
    (home / "common").mkdir(parents=True)
    return home


def _write_index(home: Path, name: str, text: str) -> None:
    root = home / name
    root.mkdir(parents=True, exist_ok=True)
    (root / "MEMORY.md").write_text(text, encoding="utf-8")


class TestRecallInjection:
    async def test_injects_common_index_after_user_message(
        self, manager, memory_home
    ):
        _write_index(memory_home, "common", "- 用户偏好中文回复")
        msgs = (Message(role="system", content="sys"), Message(role="user", content="任务"))
        result = await manager.inject_memory_recall(msgs)

        assert len(result) == 3
        hint = result[2]
        assert hint.role == "user" and hint.source == "hint"
        assert "用户偏好中文回复" in hint.content
        assert "【common 记忆索引】" in hint.content

    async def test_active_domain_index_joins_after_activation(
        self, manager, memory_home
    ):
        _write_index(memory_home, "common", "- 通用约定")
        _write_index(memory_home, "docaudit", "- GB/T 9704 验收标准")
        manager.note_domain_active("docaudit")

        result = await manager.inject_memory_recall(
            (Message(role="user", content="任务"),)
        )
        hint = result[-1].content or ""
        assert "【common 记忆索引】" in hint
        assert "【docaudit 记忆索引】" in hint
        assert "GB/T 9704" in hint

    async def test_hint_sits_before_reminder(self, manager, memory_home):
        _write_index(memory_home, "common", "索引内容")
        msgs = (
            Message(role="user", content="任务"),
            Message(role="user", content=PRE_TURN_REMINDER, source="reminder"),
        )
        result = await manager.inject_memory_recall(msgs)
        assert result[1].source == "hint"
        assert result[2].source == "reminder"

    async def test_no_reinjection_within_same_turn(self, manager, memory_home):
        _write_index(memory_home, "common", "索引内容")
        msgs = (Message(role="user", content="任务"),)
        first = await manager.inject_memory_recall(msgs)
        second = await manager.inject_memory_recall(first)
        assert second == first
        assert sum(1 for m in second if m.source == "hint") == 1

    async def test_no_injection_when_disabled(self, manager, memory_home):
        manager._auto_inject_enabled = False
        _write_index(memory_home, "common", "索引内容")
        msgs = (Message(role="user", content="任务"),)
        assert await manager.inject_memory_recall(msgs) == msgs

    async def test_no_injection_without_real_user_message(self, manager, memory_home):
        _write_index(memory_home, "common", "索引内容")
        msgs = (Message(role="user", content="任务", source="reminder"),)
        assert await manager.inject_memory_recall(msgs) == msgs

    async def test_workspace_preamble_always_present(self, manager, memory_home):
        """空索引也注入工作区段——路径是动态的，模型必须能看到（实测教训）。"""
        msgs = (Message(role="user", content="任务"),)
        result = await manager.inject_memory_recall(msgs)
        hint = result[-1].content or ""
        assert "【记忆工作区】" in hint
        assert str(manager.memory_home) in hint
        assert str(manager.session_workspace) in hint
        assert "common/" in hint
        # no index segments — only the workspace preamble
        assert "【common 记忆索引】" not in hint

    async def test_no_injection_when_index_empty_and_disabled(self, manager, memory_home):
        manager._auto_inject_enabled = False
        _write_index(memory_home, "common", "   ")
        msgs = (Message(role="user", content="任务"),)
        assert await manager.inject_memory_recall(msgs) == msgs

    async def test_per_index_cap(self, manager, memory_home):
        manager._auto_inject_max_chars = 50
        _write_index(memory_home, "common", "长" * 400)
        result = await manager.inject_memory_recall((Message(role="user", content="任务"),))
        hint = result[-1].content or ""
        assert "…" in hint
        assert "长" * 400 not in hint  # index body capped
        assert len(hint) < 500  # workspace preamble + capped index

    async def test_total_cap_skips_later_indexes(self, manager, memory_home):
        manager._auto_inject_total_chars = 60
        manager._auto_inject_max_chars = 400
        _write_index(memory_home, "common", "短索引")
        _write_index(memory_home, "docaudit", "长" * 400)
        manager.note_domain_active("docaudit")
        result = await manager.inject_memory_recall((Message(role="user", content="任务"),))
        hint = result[-1].content or ""
        assert "【common 记忆索引】" in hint
        assert "【docaudit 记忆索引】" not in hint  # total budget exhausted

    async def test_fallback_template_without_bundle(self, manager, memory_home):
        manager._recall_hint_template = None
        _write_index(memory_home, "common", "索引内容")
        result = await manager.inject_memory_recall((Message(role="user", content="任务"),))
        assert "[Recalled memory]" in (result[-1].content or "")

    async def test_injection_survives_compaction_boundary(self, manager, memory_home):
        """注入的 hint 不作压缩边界：真实任务逐字保留（边界修复联动）。"""
        _write_index(memory_home, "common", "任务背景说明")
        real = Message(role="user", content="审核这份关于XX的通知 " * 300)
        msgs = (
            Message(role="system", content="Sys"),
            Message(role="user", content="旧问题 " * 400),
            real,
        )
        injected = await manager.inject_memory_recall(msgs)
        assert len(injected) == 4 and injected[3].source == "hint"

        compacted = await manager.compact_if_needed(injected)
        assert any(m is real for m in compacted)


class TestForkPropagation:
    def test_fork_shares_workspace_and_domains_fresh_state(self, manager):
        """fork 共享记忆工作区与活跃领域快照，CompactState/去重状态全新。"""
        manager.note_domain_active("docaudit")
        manager.state.has_compacted = True
        manager._last_recall_hint_id = 12345

        child = manager.fork()

        assert isinstance(child, MemoryManager)
        assert child.memory_home == manager.memory_home
        assert child._active_domains == {"docaudit"}
        assert child.state.has_compacted is False
        assert child._last_recall_hint_id is None
        assert child._auto_inject_enabled == manager._auto_inject_enabled
        assert child._auto_inject_max_chars == manager._auto_inject_max_chars
        assert child._auto_inject_total_chars == manager._auto_inject_total_chars

    async def test_subagent_fork_write_visible_to_parent(self, manager, memory_home):
        """子代理经文件工具写的记忆，父代理注入即可见（共享工作区）。"""
        child = manager.fork()
        target = child.memory_home / "common" / "finding.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("子代理的发现", encoding="utf-8")
        _write_index(memory_home, "common", "- 子代理的发现（见 finding.md）")

        result = await manager.inject_memory_recall((Message(role="user", content="发现"),))
        assert "子代理的发现" in (result[-1].content or "")
