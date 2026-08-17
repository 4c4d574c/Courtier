"""Loopback integration tests: real TCP plugin server + host connection manager.

A real SDK plugin server (standalone serve mode) runs on 127.0.0.1 with an
ephemeral port; the host-side PluginSystem dials it like any remote plugin.
Covers the full handshake (register + mutual token auth), tool execution,
reverse host-service calls, and reconnect-after-disconnect.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import pytest
from courtier_plugin_sdk import PluginRuntime, ToolResult

from courtier.agent.core.cache_store import CacheStore
from courtier.agent.tools.registry import ToolRegistry
from courtier.plugin import PluginSystem
from courtier.plugin.manager import PluginState

TOKEN = "integration-token"


class _EchoPlugin(PluginRuntime):
    """In-process plugin server exposing an echo tool and a cache probe."""

    def _setup_handlers(self):
        runtime = self

        class EchoTool:
            name = "echo"
            display_name = "Echo"
            description = "echo back args"
            parameters = {
                "type": "object",
                "properties": {"text": {"type": "string"}},
            }

            async def execute(self, **kwargs):
                return ToolResult(success=True, data={"echo": kwargs})

        class CacheProbeTool:
            name = "cache_probe"
            display_name = "Cache Probe"
            description = "round-trips cache.persist through the host"
            parameters = {"type": "object", "properties": {}}

            async def execute(self, **kwargs):
                client = runtime.host_service_client
                if client is None:
                    return ToolResult(success=False, error="host service client missing")
                res = await client.call(
                    "cache.persist",
                    {
                        "data": {"payload": "x" * 5000},
                        "tool_name": "echo_plugin.cache_probe",
                        "force": True,
                    },
                )
                return ToolResult(
                    success=True,
                    data={"ref_id": res["ref_id"], "persisted": res["persisted"]},
                )

        self.register_tool(EchoTool())
        self.register_tool(CacheProbeTool())


def _write_manifest(plugins_dir: Path) -> None:
    plugin_dir = plugins_dir / "echo_plugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        """name: echo_plugin
version: "0.1.0"
api: "2.0"
description: "Loopback integration fixture"

dependencies:
  host_services: [cache]
  permissions: [read:cache, write:cache]
""",
        encoding="utf-8",
    )


@pytest.fixture
def plugins_dir(tmp_path: Path) -> Path:
    d = tmp_path / "plugins"
    _write_manifest(d)
    return d


@pytest.fixture
async def echo_server(monkeypatch):
    """Run the SDK plugin server; yields (runtime, port, serve_task)."""
    monkeypatch.setenv("COURTIER_PLUGIN_TOKEN", TOKEN)
    runtime = _EchoPlugin()
    task = asyncio.create_task(runtime.serve("127.0.0.1:0"))
    for _ in range(200):
        if runtime._server is not None and runtime._server.sockets:
            break
        await asyncio.sleep(0.01)
    else:  # pragma: no cover - defensive
        raise RuntimeError("plugin server did not start")
    port = runtime._server.sockets[0].getsockname()[1]
    yield runtime, port, task
    task.cancel()
    with contextlib.suppress(BaseException):
        await asyncio.wait_for(task, timeout=5)


async def _wait_state(ps: PluginSystem, name: str, state: PluginState, timeout: float = 10.0):
    """Poll get_status until the plugin reaches *state* (or fail)."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        current = ps.get_status().get(name, {}).get("state")
        if current == state.value:
            return
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"{name} did not reach {state.value}; last={current}")
        await asyncio.sleep(0.05)


def _make_system(plugins_dir: Path, port: int, tmp_path: Path) -> PluginSystem:
    return PluginSystem(
        plugins_dir=plugins_dir,
        tool_registry=ToolRegistry(),
        artifact_store=CacheStore(cache_dir=str(tmp_path / "cache")),
        endpoints={"echo_plugin": ("127.0.0.1", port)},
        token=TOKEN,
    )


class TestPluginSystemLoopback:
    @pytest.mark.asyncio
    async def test_connect_register_and_tool_roundtrip(self, plugins_dir, echo_server, tmp_path):
        _, port, _ = echo_server
        ps = _make_system(plugins_dir, port, tmp_path)
        await ps.start()
        await _wait_state(ps, "echo_plugin", PluginState.ACTIVE)

        tool = ps._manager._extension_registry._tool_registry.get("echo")
        result = await tool.execute(on_progress=lambda *_: None, text="你好")
        assert result.success is True
        assert result.data["echo"] == {"text": "你好"}

        await ps.shutdown()
        assert ps.get_status()["echo_plugin"]["state"] == "STOPPED"

    @pytest.mark.asyncio
    async def test_host_service_reverse_call_over_tcp(self, plugins_dir, echo_server, tmp_path):
        _, port, _ = echo_server
        ps = _make_system(plugins_dir, port, tmp_path)
        await ps.start()
        await _wait_state(ps, "echo_plugin", PluginState.ACTIVE)

        registry = ps._manager._extension_registry._tool_registry
        result = await registry.get("cache_probe").execute(on_progress=lambda *_: None)
        assert result.success is True
        assert result.data["persisted"] is True
        assert result.data["ref_id"].startswith("$ref:echo_plugin.cache_probe:")

        await ps.shutdown()

    @pytest.mark.asyncio
    async def test_reconnect_after_plugin_restart(
        self, plugins_dir, echo_server, tmp_path, monkeypatch
    ):
        _, port, serve_task = echo_server
        ps = _make_system(plugins_dir, port, tmp_path)
        await ps.start()
        await _wait_state(ps, "echo_plugin", PluginState.ACTIVE)

        # Kill the plugin server; the host must notice and go DISCONNECTED.
        serve_task.cancel()
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(serve_task, timeout=5)
        await _wait_state(ps, "echo_plugin", PluginState.DISCONNECTED)

        # Tool is unregistered while the plugin is down.
        registry = ps._manager._extension_registry._tool_registry
        with pytest.raises(KeyError):
            registry.get("echo")

        # Restart the plugin on the same port; the host redials by itself.
        monkeypatch.setenv("COURTIER_PLUGIN_TOKEN", TOKEN)
        runtime2 = _EchoPlugin()
        task2 = asyncio.create_task(runtime2.serve(f"127.0.0.1:{port}"))
        try:
            await _wait_state(ps, "echo_plugin", PluginState.ACTIVE, timeout=15.0)
            result = await registry.get("echo").execute(
                on_progress=lambda *_: None, text="back"
            )
            assert result.success is True
        finally:
            task2.cancel()
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(task2, timeout=5)
        await ps.shutdown()

    @pytest.mark.asyncio
    async def test_bad_plugin_token_is_blocked(self, plugins_dir, tmp_path, monkeypatch):
        # Plugin presents a different token than the host expects.
        monkeypatch.setenv("COURTIER_PLUGIN_TOKEN", "other-token")
        runtime = _EchoPlugin()
        task = asyncio.create_task(runtime.serve("127.0.0.1:0"))
        for _ in range(200):
            if runtime._server is not None and runtime._server.sockets:
                break
            await asyncio.sleep(0.01)
        port = runtime._server.sockets[0].getsockname()[1]
        try:
            ps = _make_system(plugins_dir, port, tmp_path)
            await ps.start()
            await _wait_state(ps, "echo_plugin", PluginState.BLOCKED)
            await ps.shutdown()
        finally:
            task.cancel()
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(task, timeout=5)


class TestPluginSystemEdgeCases:
    @pytest.mark.asyncio
    async def test_empty_plugins_dir(self, tmp_path):
        plugins_dir = tmp_path / "empty_plugins"
        plugins_dir.mkdir()

        ps = PluginSystem(plugins_dir=plugins_dir, endpoints={}, token=TOKEN)
        status = await ps.start()
        assert status == {}
        await ps.shutdown()

    @pytest.mark.asyncio
    async def test_shutdown_before_start_noop(self, plugins_dir):
        ps = PluginSystem(plugins_dir=plugins_dir, endpoints={}, token=TOKEN)
        await ps.shutdown()  # Should not raise
