"""Context management integration suite (§7).

Covers the seams between ContextManager and the rest of the runtime:
think-phase usage calibration & compact events (absorbing the former
test_compact_event.py) and settings-driven budget derivation.  The
run-manager state roundtrip lives in tests/agent/api/test_run_manager.py
(7.2) and the API compact-route branches in tests/agent/api/test_routes.py
(7.4/7.5), where their harnesses live.  Outline IDs cited in docstrings.
"""

from __future__ import annotations

from types import SimpleNamespace

from courtier.agent.api.services.agent_service import _context_budget_kwargs
from courtier.agent.core.context_manager import ContextManager
from courtier.agent.core.loop_phases import think_phase
from courtier.agent.core.model import MockModelClient, ModelResponse
from courtier.agent.core.state import AgentState


class _CompactingContextManager:
    """Stand-in that always shrinks the message list (real compaction)."""

    async def compact_if_needed(self, messages, *, on_compact_start=None):
        if on_compact_start is not None:
            await on_compact_start()
        return messages[:1]


class _NoopContextManager:
    """Stand-in that leaves messages untouched (no compaction)."""

    async def compact_if_needed(self, messages, *, on_compact_start=None):
        return messages


async def _noop_step(event: str, detail: str) -> None:
    return


class TestThinkPhaseUsageCalibration:
    async def test_provider_tokens_recorded_after_think(self, tmp_path):
        """7.1 think_phase 后 update_actual_usage 以 provider prompt_tokens 被调用。"""

        class _UsageModel(MockModelClient):
            async def generate(self, messages, tools=None, **kwargs):
                return ModelResponse(
                    content="Done.", tool_calls=[], usage={"prompt_tokens": 321}
                )

        cm = ContextManager(cache_dir=str(tmp_path / "cache"))
        state = AgentState.initial(task="审核文档", system_prompt="sys")
        await think_phase(
            state=state,
            model=_UsageModel(),
            tool_registry=None,
            context_manager=cm,
            recent_reasoning=[],
            on_step=_noop_step,
            on_token=None,
            on_content_token=None,
        )
        assert cm._last_actual_prompt_tokens == 321

    async def test_missing_usage_leaves_calibration_untouched(self, tmp_path):
        """7.1 补充：响应无 usage → 校准值保持 None。"""
        cm = ContextManager(cache_dir=str(tmp_path / "cache"))
        state = AgentState.initial(task="审核文档", system_prompt="sys")
        await think_phase(
            state=state,
            model=MockModelClient(tool_calls=[]),
            tool_registry=None,
            context_manager=cm,
            recent_reasoning=[],
            on_step=_noop_step,
            on_token=None,
            on_content_token=None,
        )
        assert cm._last_actual_prompt_tokens is None


class TestBudgetDerivation:
    def test_defaults_match_module_constants(self):
        """7.3 (迁移) 无 settings 属性时按文档化默认值推导。"""
        assert _context_budget_kwargs(SimpleNamespace()) == {
            "max_context_tokens": 24_576,  # 0.75 × 32768
            "micro_compact_tokens": 19_660,  # 0.60 × 32768
            "compact_target_tokens": 16_384,  # 0.50 × 32768
            "recent_tool_results_tokens": 4_000,
            "preview_max_chars": 1_000,
            "media_image_tokens": 1024,
            "media_audio_tokens_per_second": 40.0,
            "media_video_tokens_per_second": 200.0,
        }

    def test_settings_overrides(self):
        """7.3 settings 显式配置覆盖比例与固定值。"""
        settings = SimpleNamespace(
            llm_context_window_tokens=1000,
            context_budget_ratio=0.5,
            context_micro_compact_ratio=0.4,
            context_compact_target_ratio=0.3,
            context_recent_tool_results_tokens=7,
            context_preview_max_chars=11,
        )
        settings.media_image_token_estimate = 12
        settings.media_audio_tokens_per_second = 13.0
        settings.media_video_tokens_per_second = 14.0
        assert _context_budget_kwargs(settings) == {
            "max_context_tokens": 500,
            "micro_compact_tokens": 400,
            "compact_target_tokens": 300,
            "recent_tool_results_tokens": 7,
            "preview_max_chars": 11,
            "media_image_tokens": 12,
            "media_audio_tokens_per_second": 13.0,
            "media_video_tokens_per_second": 14.0,
        }

    def test_zero_window_extreme_config(self):
        """7.3 (极端·pin) 窗口为 0 → 三个预算全为 0（每轮必压），不崩。

        已知行为：窗口 0 是无意义配置，但推导层不设防；预算为 0 时
        压缩每次触发、无新历史时被 skip 守卫拦住，不会死循环。
        """
        kwargs = _context_budget_kwargs(SimpleNamespace(llm_context_window_tokens=0))
        assert kwargs["max_context_tokens"] == 0
        assert kwargs["micro_compact_tokens"] == 0
        assert kwargs["compact_target_tokens"] == 0


# -- 7.6 迁移自 tests/agent/core/test_compact_event.py ------------------------


class TestCompactEvents:
    async def test_compact_event_emitted_when_history_shrinks(self):
        """7.6 (迁移) think_phase 在压缩发生时按序发出 compacting/compact 事件。"""
        events: list[tuple[str, str]] = []

        async def on_step(event: str, detail: str) -> None:
            events.append((event, detail))

        state = AgentState.initial(task="审核文档", system_prompt="sys")
        assert len(state.messages) == 2

        result = await think_phase(
            state=state,
            model=MockModelClient(tool_calls=[]),
            tool_registry=None,
            context_manager=_CompactingContextManager(),
            recent_reasoning=[],
            on_step=on_step,
            on_token=None,
            on_content_token=None,
        )

        compact_events = [d for e, d in events if e == "compact"]
        assert compact_events == ["2 条消息 → 1 条"]
        event_names = [e for e, _ in events]
        assert event_names.index("compacting") < event_names.index("compact")
        assert result.state.messages[0].content == "sys"
        assert result.state.messages[-1].role == "assistant"

    async def test_compact_event_published_on_event_bus(self):
        """7.6 (迁移) 事件总线驱动路径同样收到压缩通知（stream_service 依赖）。"""
        from courtier.agent.core.event_bus import EventBus
        from courtier.agent.core.loop import agent_loop

        bus = EventBus()
        sub = bus.subscribe()
        state = AgentState.initial(task="审核文档", system_prompt="sys")
        await agent_loop(
            state=state,
            model=MockModelClient(tool_calls=[]),
            tool_registry=None,
            context_manager=_CompactingContextManager(),
            event_bus=bus,
        )

        events = []
        while not sub.queue.empty():
            events.append(sub.queue.get_nowait())
        compacted = [e for e in events if e.type == "context.compacted"]
        assert len(compacted) == 1
        assert compacted[0].payload["detail"].endswith("→ 1 条")
        compacting = [e for e in events if e.type == "context.compacting"]
        assert len(compacting) == 1
        assert events.index(compacting[0]) < events.index(compacted[0])

    async def test_no_compact_event_when_unchanged(self):
        """7.6 (迁移) 未发生压缩时不得发出 compacting/compact 通知。"""
        events: list[tuple[str, str]] = []

        async def on_step(event: str, detail: str) -> None:
            events.append((event, detail))

        state = AgentState.initial(task="审核文档", system_prompt="sys")
        await think_phase(
            state=state,
            model=MockModelClient(tool_calls=[]),
            tool_registry=None,
            context_manager=_NoopContextManager(),
            recent_reasoning=[],
            on_step=on_step,
            on_token=None,
            on_content_token=None,
        )

        assert [d for e, d in events if e == "compact"] == []
        assert [d for e, d in events if e == "compacting"] == []


class TestMemoryRecallInjection:
    """7.7 think_phase 的记忆自动注入（MemoryManager 鸭子类型调用）。"""

    async def test_think_phase_injects_memory_hint(self, tmp_path):
        from courtier.agent.core.memory_manager import MemoryManager

        mgr = MemoryManager(cache_dir=str(tmp_path / "cache"), session_id="s1")

        async def provider(domains):
            return {
                "user": [{"title": "上季度结论", "domain": "common",
                          "content": "季度报告结论是收入增长"}],
                "global": [],
            }

        mgr._memory_index_provider = provider
        state = AgentState.initial(task="季度报告审核", system_prompt="sys")

        result = await think_phase(
            state=state,
            model=MockModelClient(tool_calls=[]),
            tool_registry=None,
            context_manager=mgr,
            recent_reasoning=[],
            on_step=_noop_step,
            on_token=None,
            on_content_token=None,
        )

        hints = [m for m in result.state.messages if m.source == "hint"]
        assert len(hints) == 1
        assert "季度报告结论" in (hints[0].content or "")

    async def test_think_phase_does_not_reinject_next_step(self, tmp_path):
        """同一条 hint 消息已在真实用户消息之后 → 后续 think 不再注入。"""
        from courtier.agent.core.memory_manager import MemoryManager

        mgr = MemoryManager(cache_dir=str(tmp_path / "cache"), session_id="s1")

        async def provider(domains):
            return {
                "user": [{"title": "上季度结论", "domain": "common",
                          "content": "季度报告结论是收入增长"}],
                "global": [],
            }

        mgr._memory_index_provider = provider
        state = AgentState.initial(task="季度报告审核", system_prompt="sys")

        first = await think_phase(
            state=state,
            model=MockModelClient(tool_calls=[]),
            tool_registry=None,
            context_manager=mgr,
            recent_reasoning=[],
            on_step=_noop_step,
            on_token=None,
            on_content_token=None,
        )
        second = await think_phase(
            state=first.state,
            model=MockModelClient(tool_calls=[]),
            tool_registry=None,
            context_manager=mgr,
            recent_reasoning=[],
            on_step=_noop_step,
            on_token=None,
            on_content_token=None,
        )
        assert len([m for m in second.state.messages if m.source == "hint"]) == 1

    async def test_plain_context_manager_unaffected(self, tmp_path):
        """普通 ContextManager 没有 inject_memory_recall → 消息不动。"""
        cm = ContextManager(cache_dir=str(tmp_path / "cache"))
        state = AgentState.initial(task="审核文档", system_prompt="sys")
        result = await think_phase(
            state=state,
            model=MockModelClient(tool_calls=[]),
            tool_registry=None,
            context_manager=cm,
            recent_reasoning=[],
            on_step=_noop_step,
            on_token=None,
            on_content_token=None,
        )
        assert all(m.source != "hint" for m in result.state.messages)
