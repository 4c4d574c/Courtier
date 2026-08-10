"""Tests for Plugin health check protocol.

Covers health check RPC, 3-strike restart logic, and timeout
propagation from plugin.yaml.
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from courtier.plugin.client import JSONRPCClient
from courtier.plugin.manager import PluginProcess, PluginState, ProcessManager
from courtier.plugin.manifest import PluginManifest


def _minimal_manifest(**overrides) -> PluginManifest:
    """Create a valid minimal PluginManifest with optional overrides."""
    data = {
        "name": "test_plugin",
        "version": "0.1.0",
        "api": "1.0",
    }
    data.update(overrides)
    manifest = PluginManifest.model_validate(data)
    manifest.dir = Path("/tmp")
    return manifest


def _make_proc(manifest: PluginManifest | None = None) -> PluginProcess:
    if manifest is None:
        manifest = _minimal_manifest()
    proc = PluginProcess(
        name=manifest.name,
        manifest=manifest,
        plugin_dir=manifest.dir or Path("/tmp"),
    )
    proc._client = MagicMock()
    return proc


class TestHealthCheck:
    """Tests for ProcessManager.health_check()."""

    async def test_health_check_returns_ok(self):
        """health_check returns True when plugin responds with status ok."""
        manifest = _minimal_manifest()
        proc = _make_proc(manifest)
        proc._client.call = AsyncMock(return_value={"status": "ok", "dependencies": {}})

        manager = ProcessManager(
            plugin_dir=Path("/tmp"),
            extension_registry=MagicMock(),
        )

        assert await manager.health_check(proc) is True
        proc._client.call.assert_awaited_once_with("plugin.health", timeout=5.0)

    async def test_health_check_returns_false_on_exception(self):
        """health_check returns False when call raises an exception."""
        proc = _make_proc()
        proc._client.call = AsyncMock(side_effect=RuntimeError("connection lost"))

        manager = ProcessManager(
            plugin_dir=Path("/tmp"),
            extension_registry=MagicMock(),
        )

        assert await manager.health_check(proc) is False

    async def test_health_check_returns_false_on_unexpected_response(self):
        """health_check returns False when plugin returns unexpected status."""
        proc = _make_proc()
        proc._client.call = AsyncMock(return_value={"status": "degraded", "dependencies": {}})

        manager = ProcessManager(
            plugin_dir=Path("/tmp"),
            extension_registry=MagicMock(),
        )

        assert await manager.health_check(proc) is False

    async def test_health_legacy_string_response(self):
        """Backward compatibility: 'ok' string still works."""
        proc = _make_proc()
        proc._client.call = AsyncMock(return_value="ok")

        manager = ProcessManager(
            plugin_dir=Path("/tmp"),
            extension_registry=MagicMock(),
        )

        assert await manager.health_check(proc) is True


class TestHealthLoopThreeStrike:
    """Tests for the 3-consecutive-failure restart logic."""

    async def test_three_failures_triggers_restart(self):
        """After 3 consecutive failures, _on_crash is called."""
        manifest = _minimal_manifest()
        proc = PluginProcess(
            name=manifest.name,
            manifest=manifest,
            plugin_dir=manifest.dir or Path("/tmp"),
        )
        proc.state = PluginState.ACTIVE
        proc._health_failures = 2  # Two failures already recorded
        proc._client = MagicMock()

        manager = ProcessManager(
            plugin_dir=Path("/tmp"),
            extension_registry=MagicMock(),
            health_interval=0.01,
        )
        manager._processes[manifest.name] = proc

        manager.health_check = AsyncMock(return_value=False)
        manager._on_crash = AsyncMock()

        health_task = asyncio.create_task(manager._health_loop(proc))

        # Give the loop enough time to run at least one check cycle
        await asyncio.sleep(0.1)

        health_task.cancel()
        try:
            await health_task
        except (asyncio.CancelledError, Exception):
            pass

        # After the third failure, _on_crash should have been called
        manager._on_crash.assert_awaited_once_with(proc)

    async def test_successful_check_resets_failure_counter(self):
        """A successful health check resets the failure counter."""
        manifest = _minimal_manifest()
        proc = PluginProcess(
            name=manifest.name,
            manifest=manifest,
            plugin_dir=manifest.dir or Path("/tmp"),
        )
        proc.state = PluginState.ACTIVE
        proc._health_failures = 2  # Two previous failures
        proc._client = MagicMock()

        manager = ProcessManager(
            plugin_dir=Path("/tmp"),
            extension_registry=MagicMock(),
            health_interval=0.01,
        )
        manager._processes[manifest.name] = proc

        # Make health check succeed
        manager.health_check = AsyncMock(return_value=True)

        health_task = asyncio.create_task(manager._health_loop(proc))

        await asyncio.sleep(0.1)

        health_task.cancel()
        try:
            await health_task
        except (asyncio.CancelledError, Exception):
            pass

        # Counter should be reset to 0
        assert proc._health_failures == 0

    async def test_does_not_restart_on_single_failure(self):
        """A single failure (without reaching 3) does NOT trigger restart."""
        manifest = _minimal_manifest()
        proc = PluginProcess(
            name=manifest.name,
            manifest=manifest,
            plugin_dir=manifest.dir or Path("/tmp"),
        )
        proc.state = PluginState.ACTIVE
        proc._health_failures = 0
        proc._client = MagicMock()

        manager = ProcessManager(
            plugin_dir=Path("/tmp"),
            extension_registry=MagicMock(),
            health_interval=0.01,
        )
        manager._processes[manifest.name] = proc

        # Health check fails once, then succeeds — counter should
        # go to 1 then reset to 0 before reaching the 3-strike limit.
        call_count = 0

        async def _failing_once(proc):
            nonlocal call_count
            call_count += 1
            return call_count >= 2  # Fail on first call, succeed after

        manager.health_check = _failing_once
        manager._on_crash = AsyncMock()

        health_task = asyncio.create_task(manager._health_loop(proc))

        await asyncio.sleep(0.15)  # Enough for several check cycles

        health_task.cancel()
        try:
            await health_task
        except (asyncio.CancelledError, Exception):
            pass

        # Counter should be 0 (reset by the successful check)
        assert proc._health_failures == 0
        manager._on_crash.assert_not_awaited()


class TestTimeoutFromManifest:
    """Tests for timeout_ms propagation from plugin.yaml."""

    def test_manifest_default_timeout(self):
        """PluginManifest defaults timeout_ms to 120_000."""
        m = _minimal_manifest()
        assert m.timeout_ms == 120_000

    def test_manifest_custom_timeout(self):
        """PluginManifest accepts a custom timeout_ms."""
        m = _minimal_manifest(timeout_ms=5_000)
        assert m.timeout_ms == 5_000

    def test_client_default_timeout_constructor(self):
        """JSONRPCClient stores default_timeout from constructor."""
        reader = AsyncMock()
        writer = AsyncMock()

        client = JSONRPCClient(reader, writer, "test_plugin", default_timeout=5.0)
        assert client._default_timeout == 5.0

        client2 = JSONRPCClient(reader, writer, "test_plugin", default_timeout=120.0)
        assert client2._default_timeout == 120.0
