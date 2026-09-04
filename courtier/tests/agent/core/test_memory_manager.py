"""MemoryManager suite (DB-backed layered memory + recall injection).

Memory entries live in the host database and reach the manager through an
injected async provider (``domains -> {"user": [...], "global": [...]}``);
the manager's own logic under test here is the recall injection (segment
shaping, caps, dedup) and fork config propagation.  Working-tier
(compaction) coverage lives in test_context_manager.py; the provider's
query semantics live in the memory service tests.
"""

from __future__ import annotations

import pytest

from courtier.agent.core.memory_manager import MemoryManager
from courtier.agent.core.state import Message

from .conftest import PRE_TURN_REMINDER


def _entry(title: str, content: str, domain: str = "common") -> dict:
    return {
        "id": 1,
        "layer": "user",
        "ownerId": 7,
        "domain": domain,
        "title": title,
        "content": content,
        "createdAt": "2026-09-04T00:00:00",
        "updatedAt": "2026-09-04T00:00:00",
    }


def _provider(user=None, glob=None, seen_domains=None):
    """Provider stub recording the requested active domains."""

    async def provider(domains):
        if seen_domains is not None:
            seen_domains.extend(domains)
        return {"user": list(user or []), "global": list(glob or [])}

    return provider


@pytest.fixture
def manager(tmp_path):
    return MemoryManager(cache_dir=str(tmp_path / "cache"), session_id="sess_1")


class TestRecallInjection:
    async def test_injects_user_index_after_user_message(self, manager):
        manager._memory_index_provider = _provider(
            user=[_entry("回复语言偏好", "用户偏好中文回复")]
        )
        msgs = (Message(role="system", content="sys"), Message(role="user", content="任务"))
        result = await manager.inject_memory_recall(msgs)

        assert len(result) == 3
        hint = result[2]
        assert hint.role == "user" and hint.source == "hint"
        assert "用户偏好中文回复" in hint.content
        assert "【用户记忆】" in hint.content

    async def test_user_layer_first_then_global(self, manager):
        manager._memory_index_provider = _provider(
            user=[_entry("偏好", "简洁")],
            glob=[_entry("验收标准", "GB/T 9704 格式要点", domain="docaudit")],
        )
        result = await manager.inject_memory_recall((Message(role="user", content="任务"),))
        hint = result[-1].content or ""
        assert hint.index("【用户记忆】") < hint.index("【全局共享记忆】")
        assert "GB/T 9704" in hint
        assert "（docaudit）" in hint  # 非通用条目标注领域包

    async def test_active_domains_passed_to_provider(self, manager):
        seen: list[str] = []
        manager._memory_index_provider = _provider(user=[_entry("偏好", "简洁")], seen_domains=seen)
        manager.note_domain_active("docaudit")
        manager.note_domain_active("search")
        await manager.inject_memory_recall((Message(role="user", content="任务"),))
        assert seen == ["docaudit", "search"]  # sorted

    async def test_hint_sits_before_reminder(self, manager):
        manager._memory_index_provider = _provider(user=[_entry("偏好", "简洁")])
        msgs = (
            Message(role="user", content="任务"),
            Message(role="user", content=PRE_TURN_REMINDER, source="reminder"),
        )
        result = await manager.inject_memory_recall(msgs)
        assert result[1].source == "hint"
        assert result[2].source == "reminder"

    async def test_no_reinjection_within_same_turn(self, manager):
        manager._memory_index_provider = _provider(user=[_entry("偏好", "简洁")])
        msgs = (Message(role="user", content="任务"),)
        first = await manager.inject_memory_recall(msgs)
        second = await manager.inject_memory_recall(first)
        assert second == first
        assert sum(1 for m in second if m.source == "hint") == 1

    async def test_no_injection_when_disabled(self, manager):
        manager._auto_inject_enabled = False
        manager._memory_index_provider = _provider(user=[_entry("偏好", "简洁")])
        msgs = (Message(role="user", content="任务"),)
        assert await manager.inject_memory_recall(msgs) == msgs

    async def test_no_injection_without_provider(self, manager):
        """匿名会话 / 记忆插件缺席 → 无 provider → 完全不注入。"""
        msgs = (Message(role="user", content="任务"),)
        assert await manager.inject_memory_recall(msgs) == msgs

    async def test_no_injection_without_real_user_message(self, manager):
        manager._memory_index_provider = _provider(user=[_entry("偏好", "简洁")])
        msgs = (Message(role="user", content="任务", source="reminder"),)
        assert await manager.inject_memory_recall(msgs) == msgs

    async def test_no_injection_when_index_empty(self, manager):
        """空索引零成本：不注入任何 hint（没有可用面就不占预算）。"""
        manager._memory_index_provider = _provider()
        msgs = (Message(role="user", content="任务"),)
        assert await manager.inject_memory_recall(msgs) == msgs

    async def test_provider_failure_degrades_to_empty(self, manager):
        """provider 抛错 fail-open：轮次照常，只是没有记忆段。"""

        async def broken(domains):
            raise RuntimeError("db down")

        manager._memory_index_provider = broken
        msgs = (Message(role="user", content="任务"),)
        assert await manager.inject_memory_recall(msgs) == msgs

    async def test_per_entry_cap(self, manager):
        manager._auto_inject_max_chars = 50
        manager._memory_index_provider = _provider(user=[_entry("长条目", "长" * 400)])
        result = await manager.inject_memory_recall((Message(role="user", content="任务"),))
        hint = result[-1].content or ""
        assert "…" in hint
        assert "长" * 400 not in hint

    async def test_total_cap_skips_later_segments(self, manager):
        manager._auto_inject_total_chars = 60
        manager._memory_index_provider = _provider(
            user=[_entry("偏好", "短")],
            glob=[_entry("标准", "长" * 400)],
        )
        result = await manager.inject_memory_recall((Message(role="user", content="任务"),))
        hint = result[-1].content or ""
        assert "【用户记忆】" in hint
        assert "【全局共享记忆】" not in hint  # total budget exhausted

    async def test_sync_provider_supported(self, manager):
        """provider 允许返回普通 dict（鸭子类型，不强制 async）。"""
        manager._memory_index_provider = lambda domains: {
            "user": [_entry("偏好", "简洁")],
            "global": [],
        }
        result = await manager.inject_memory_recall((Message(role="user", content="任务"),))
        assert "简洁" in (result[-1].content or "")

    async def test_fallback_template_without_bundle(self, manager):
        manager._recall_hint_template = None
        manager._memory_index_provider = _provider(user=[_entry("偏好", "简洁")])
        result = await manager.inject_memory_recall((Message(role="user", content="任务"),))
        assert "[Recalled memory]" in (result[-1].content or "")

    async def test_injection_survives_compaction_boundary(self, manager):
        """注入的 hint 不作压缩边界：真实任务逐字保留（边界修复联动）。"""
        manager._memory_index_provider = _provider(user=[_entry("偏好", "任务背景说明")])
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
    def test_fork_shares_provider_and_domains_fresh_state(self, manager, tmp_path):
        """fork 共享 provider 与活跃领域快照，CompactState/去重状态全新。"""
        manager._memory_index_provider = _provider(user=[_entry("偏好", "简洁")])
        manager.note_domain_active("docaudit")
        manager.state.has_compacted = True
        manager._last_recall_hint_id = 12345

        child = manager.fork()

        assert isinstance(child, MemoryManager)
        assert child._memory_index_provider is manager._memory_index_provider
        assert child._active_domains == {"docaudit"}
        assert child.state.has_compacted is False
        assert child._last_recall_hint_id is None
        assert child._auto_inject_enabled == manager._auto_inject_enabled
        assert child._auto_inject_max_chars == manager._auto_inject_max_chars
        assert child._auto_inject_total_chars == manager._auto_inject_total_chars

    async def test_subagent_fork_shares_provider_view(self, manager):
        """子代理 fork 与父代理走同一 provider——记忆视图一致。"""
        child = manager.fork()
        manager._memory_index_provider = _provider(
            user=[_entry("子代理的发现", "发现内容")]
        )
        child._memory_index_provider = manager._memory_index_provider

        result = await child.inject_memory_recall((Message(role="user", content="发现"),))
        assert "发现内容" in (result[-1].content or "")
