"""Tests for the SDK standalone TCP serve mode and auth handshake."""

from __future__ import annotations

import asyncio
import contextlib
import json

import pytest
from courtier_plugin_sdk.protocol import AUTH_ERROR, METHOD_PLUGIN_AUTH
from courtier_plugin_sdk.runtime import PluginRuntime

TOKEN = "test-secret"


class _EchoPlugin(PluginRuntime):
    def register_capabilities(self):
        return {"capabilities": [], "system_prompt": "echo"}

    def _setup_handlers(self):
        class EchoTool:
            name = "echo"
            description = "echo back args"
            parameters = {"type": "object", "properties": {}}

            async def execute(self, **kwargs):
                from courtier_plugin_sdk import ToolResult

                return ToolResult(success=True, data={"echo": kwargs})

        self.register_tool(EchoTool())


@pytest.fixture
async def running_plugin(monkeypatch):
    """Start an _EchoPlugin server on an ephemeral loopback port."""
    monkeypatch.setenv("COURTIER_PLUGIN_TOKEN", TOKEN)
    runtime = _EchoPlugin()
    task = asyncio.create_task(runtime.serve("127.0.0.1:0"))
    # Wait until the server is actually listening.
    for _ in range(100):
        if runtime._server is not None and runtime._server.sockets:
            break
        await asyncio.sleep(0.01)
    else:  # pragma: no cover - defensive
        raise RuntimeError("plugin server did not start")
    port = runtime._server.sockets[0].getsockname()[1]
    yield runtime, port
    task.cancel()
    with contextlib.suppress(BaseException):
        await asyncio.wait_for(task, timeout=5)


async def _connect(port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, dict]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    line = await asyncio.wait_for(reader.readline(), 5)
    register = json.loads(line)
    return reader, writer, register


async def _call(writer, reader, req_id: int, method: str, params: dict | None = None) -> dict:
    payload = json.dumps({"id": req_id, "method": method, "params": params or {}})
    writer.write((payload + "\n").encode())
    await writer.drain()
    return json.loads(await asyncio.wait_for(reader.readline(), 5))


class TestRegisterOnConnect:
    @pytest.mark.asyncio
    async def test_register_arrives_immediately_with_token(self, running_plugin):
        _, port = running_plugin
        reader, writer, register = await _connect(port)
        assert register["method"] == "plugin.register"
        assert register["params"]["token"] == TOKEN
        names = [c["name"] for c in register["params"]["capabilities"]]
        assert "echo" in names
        writer.close()


class TestAuthGate:
    @pytest.mark.asyncio
    async def test_preauth_requests_rejected(self, running_plugin):
        _, port = running_plugin
        reader, writer, _ = await _connect(port)
        resp = await _call(writer, reader, 1, "tool.list")
        assert resp["error"]["code"] == AUTH_ERROR
        # Health checks are likewise gated.
        resp = await _call(writer, reader, 2, "plugin.health")
        assert resp["error"]["code"] == AUTH_ERROR
        writer.close()

    @pytest.mark.asyncio
    async def test_wrong_token_closes_connection(self, running_plugin):
        _, port = running_plugin
        reader, writer, _ = await _connect(port)
        resp = await _call(writer, reader, 1, METHOD_PLUGIN_AUTH, {"token": "nope"})
        assert resp["error"]["code"] == AUTH_ERROR
        # Plugin closes the connection after a failed auth.
        assert await asyncio.wait_for(reader.readline(), 5) == b""

    @pytest.mark.asyncio
    async def test_correct_token_unlocks_methods(self, running_plugin):
        _, port = running_plugin
        reader, writer, _ = await _connect(port)
        resp = await _call(writer, reader, 1, METHOD_PLUGIN_AUTH, {"token": TOKEN})
        assert resp["result"] == {"ok": True}

        resp = await _call(writer, reader, 2, "tool.list")
        assert resp["result"] == ["echo"]

        resp = await _call(writer, reader, 3, "plugin.health")
        assert resp["result"]["status"] == "ok"

        resp = await _call(
            writer, reader, 4, "tool.execute", {"tool": "echo", "args": {"x": 1}}
        )
        assert resp["result"]["success"] is True
        assert resp["result"]["data"]["echo"] == {"x": 1}
        writer.close()

    @pytest.mark.asyncio
    async def test_second_connection_has_independent_auth(self, running_plugin):
        _, port = running_plugin
        reader_a, writer_a, _ = await _connect(port)
        reader_b, writer_b, _ = await _connect(port)

        # Authenticate A only; B stays gated.
        await _call(writer_a, reader_a, 1, METHOD_PLUGIN_AUTH, {"token": TOKEN})
        resp_a = await _call(writer_a, reader_a, 2, "tool.list")
        assert resp_a["result"] == ["echo"]
        resp_b = await _call(writer_b, reader_b, 1, "tool.list")
        assert resp_b["error"]["code"] == AUTH_ERROR

        writer_a.close()
        writer_b.close()


class TestGracefulShutdown:
    @pytest.mark.asyncio
    async def test_shutdown_with_open_connection(self, running_plugin):
        """SIGTERM-style stop must finish even while a client stays connected.

        Regression: Server.wait_closed() (3.12+) also waits on open
        connections, so closing them must happen before waiting.
        """
        runtime, port = running_plugin
        reader, writer, register = await _connect(port)
        assert register["method"] == "plugin.register"
        # Do NOT close the client connection; trigger shutdown server-side.
        runtime._stop.set()
        # The server must close our connection within the shutdown budget —
        # the client observes EOF.
        assert await asyncio.wait_for(reader.readline(), 5) == b""


class TestStartupValidation:
    @pytest.mark.asyncio
    async def test_missing_token_refuses_to_start(self, monkeypatch):
        monkeypatch.delenv("COURTIER_PLUGIN_TOKEN", raising=False)
        with pytest.raises(SystemExit):
            await _EchoPlugin().serve("127.0.0.1:0")

    def test_listen_resolution_priority(self, monkeypatch):
        # CLI arg > env > manifest port.
        assert PluginRuntime._resolve_listen("1.2.3.4:9000", {}) == ("1.2.3.4", 9000)
        monkeypatch.setenv("COURTIER_PLUGIN_LISTEN", "127.0.0.1:9100")
        assert PluginRuntime._resolve_listen(None, {}) == ("127.0.0.1", 9100)
        monkeypatch.delenv("COURTIER_PLUGIN_LISTEN")
        assert PluginRuntime._resolve_listen(None, {"runtime": {"port": 9107}}) == (
            "0.0.0.0",
            9107,
        )
        with pytest.raises(SystemExit):
            PluginRuntime._resolve_listen(None, {})
        with pytest.raises(SystemExit):
            PluginRuntime._resolve_listen("no-port-here", {})

    def test_manifest_env_literals_become_defaults(self, monkeypatch):
        monkeypatch.delenv("SEARCH_KNN_K", raising=False)
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        manifest = {
            "runtime": {
                "env": {
                    "SEARCH_KNN_K": "50",  # literal → default
                    "LLM_API_KEY": "${ENV:LLM_API_KEY}",  # legacy marker → ignored
                }
            }
        }
        PluginRuntime()._apply_manifest_env(manifest)
        import os

        assert os.environ["SEARCH_KNN_K"] == "50"
        assert "LLM_API_KEY" not in os.environ
