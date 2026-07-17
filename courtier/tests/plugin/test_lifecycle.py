"""Tests for PluginLifecycle."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from courtier.agent.core.capability import Capability, CapabilityRegistry
from courtier.plugin.lifecycle import HealthStatus, PluginHandle, PluginLifecycle


class TestPluginLifecycle:
    @pytest.mark.asyncio
    async def test_tracks_process_and_resets_health(self):
        registry = CapabilityRegistry()
        lifecycle = PluginLifecycle(capability_registry=registry)

        handle = PluginHandle(provider="p1", process=MagicMock())
        lifecycle.track_process(handle)
        lifecycle.reset_health("p1")

        assert lifecycle.get_handle("p1") is handle
        assert handle.restart_count == 0
        assert lifecycle.health_check("p1") == HealthStatus.HEALTHY

    @pytest.mark.asyncio
    async def test_untrack_cancels_pending_restart(self):
        lifecycle = PluginLifecycle()
        handle = PluginHandle(provider="p1", process=MagicMock())
        lifecycle.track_process(handle)

        await lifecycle.schedule_restart("p1", delay=60.0)
        task = lifecycle._restart_tasks.get("p1")
        assert task is not None

        lifecycle.untrack_process("p1")
        assert lifecycle.get_handle("p1") is None
        # Give the event loop a chance to process the cancellation.
        await asyncio.sleep(0)
        assert task.cancelled()

    @pytest.mark.asyncio
    async def test_schedule_restart_calls_callback(self):
        callback = AsyncMock()
        lifecycle = PluginLifecycle(restart_callback=callback)
        lifecycle.track_process(PluginHandle(provider="p1", process=MagicMock()))

        await lifecycle.schedule_restart("p1", delay=0.0)
        await asyncio.sleep(0.05)

        callback.assert_awaited_once_with("p1")

    def test_should_restart_respects_max_restarts(self):
        lifecycle = PluginLifecycle(max_restarts=2)
        assert lifecycle.should_restart("p1", started_at=10.0, restart_count=0) is True
        assert lifecycle.should_restart("p1", started_at=10.0, restart_count=2) is False

    def test_should_restart_respects_immediate_crash_window(self):
        lifecycle = PluginLifecycle(immediate_crash_window=5.0)
        # A process that started just now and crashed immediately should not restart.
        just_now = asyncio.get_event_loop().time()
        assert lifecycle.should_restart("p1", started_at=just_now, restart_count=0) is False

    @pytest.mark.asyncio
    async def test_listener_schedules_restart_on_unregister(self):
        callback = AsyncMock()
        registry = CapabilityRegistry()
        lifecycle = PluginLifecycle(
            capability_registry=registry,
            restart_callback=callback,
            max_restarts=3,
            immediate_crash_window=0.0,
        )
        lifecycle.track_process(
            PluginHandle(
                provider="p1",
                process=MagicMock(),
                started_at=asyncio.get_event_loop().time(),
            )
        )

        cap = Capability(type="tool", name="t1", provider="p1")
        registry.register(cap)
        registry.unregister("tool", "t1")

        # First restart uses a 1s exponential backoff.
        await asyncio.sleep(1.1)
        callback.assert_awaited_once_with("p1")

    @pytest.mark.asyncio
    async def test_listener_marks_fatal_after_max_restarts(self):
        registry = CapabilityRegistry()
        lifecycle = PluginLifecycle(
            capability_registry=registry,
            max_restarts=1,
            immediate_crash_window=0.0,
        )
        started_at = asyncio.get_event_loop().time()
        lifecycle.track_process(
            PluginHandle(provider="p1", process=MagicMock(), started_at=started_at, restart_count=1)
        )

        cap = Capability(type="tool", name="t1", provider="p1")
        registry.register(cap)
        registry.unregister("tool", "t1")

        await asyncio.sleep(0.05)
        assert lifecycle.health_check("p1") == HealthStatus.UNHEALTHY

    @pytest.mark.asyncio
    async def test_listener_ignores_register_event(self):
        callback = AsyncMock()
        registry = CapabilityRegistry()
        lifecycle = PluginLifecycle(
            capability_registry=registry,
            restart_callback=callback,
        )
        lifecycle.track_process(
            PluginHandle(provider="p1", process=MagicMock())
        )

        cap = Capability(type="tool", name="t1", provider="p1")
        registry.register(cap)

        await asyncio.sleep(0.05)
        callback.assert_not_awaited()
        assert lifecycle.get_handle("p1").restart_count == 0

    @pytest.mark.asyncio
    async def test_shutdown_clears_handles_and_tasks(self):
        lifecycle = PluginLifecycle()
        lifecycle.track_process(PluginHandle(provider="p1", process=MagicMock()))
        await lifecycle.schedule_restart("p1", delay=60.0)

        await lifecycle.shutdown()

        assert lifecycle.get_handle("p1") is None
        assert not lifecycle._restart_tasks
