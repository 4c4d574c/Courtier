"""Tests for PluginRuntime SDK."""

import asyncio
import json
import threading
import time

import pytest

from courtier.plugin.sdk.protocol import METHOD_TOOL_EXECUTE
from courtier.plugin.sdk.runtime import PluginRuntime, _sanitize_rpc_log


class TestPluginRuntime:
    """Shared writer that decodes bytes written by _send_line.

    PluginRuntime now writes UTF-8 bytes to match the host's binary
    asyncio.StreamReader.  Test writers decode back to str for assertions.
    """

    @staticmethod
    def _make_writer(output_lines: list[str]):
        class TestWriter:
            def write(self, data: str | bytes):
                text = data.decode("utf-8") if isinstance(data, bytes) else data
                output_lines.append(text)

            def flush(self):
                pass

        return TestWriter()

    @pytest.mark.asyncio
    async def test_startup_sends_register_notification(self):
        runtime = PluginRuntime()

        output_lines: list[str] = []
        input_queue: asyncio.Queue[str] = asyncio.Queue()

        runtime._reader = input_queue
        runtime._writer = self._make_writer(output_lines)

        # Feed a health check and EOF
        input_queue.put_nowait(
            json.dumps({"id": 1, "method": "plugin.health", "params": {}})
        )
        input_queue.put_nowait("")  # EOF

        await runtime.run()

        # First output should be the register notification
        assert len(output_lines) >= 1
        first = json.loads(output_lines[0])
        assert first["method"] == "plugin.register"
        assert "capabilities" in first["params"]

    @pytest.mark.asyncio
    async def test_handles_health_check(self):
        runtime = PluginRuntime()

        output_lines: list[str] = []
        input_queue: asyncio.Queue[str] = asyncio.Queue()

        runtime._reader = input_queue
        runtime._writer = self._make_writer(output_lines)

        # Feed health check + EOF
        input_queue.put_nowait(
            json.dumps({"id": 1, "method": "plugin.health", "params": {}})
        )
        input_queue.put_nowait("")

        await runtime.run()

        # Should have a health response after register notification
        responses = [
            json.loads(line) for line in output_lines if '"id"' in line and '"method"' not in line
        ]
        health_resp = next(
            (
                r
                for r in responses
                if isinstance(r.get("result"), dict) and r["result"].get("status") == "ok"
            ),
            None,
        )
        assert health_resp is not None, f"No health response found in {responses}"

    @pytest.mark.asyncio
    async def test_handles_unknown_method_with_error(self):
        runtime = PluginRuntime()

        output_lines: list[str] = []
        input_queue: asyncio.Queue[str] = asyncio.Queue()

        runtime._reader = input_queue
        runtime._writer = self._make_writer(output_lines)

        input_queue.put_nowait(
            json.dumps({"id": 2, "method": "unknown.method", "params": {}})
        )
        input_queue.put_nowait("")

        await runtime.run()

        responses = [json.loads(line) for line in output_lines if '"id"' in line]
        err_resp = next((r for r in responses if r.get("id") == 2), None)
        assert err_resp is not None
        assert "error" in err_resp

    @pytest.mark.asyncio
    async def test_malformed_json_is_skipped(self):
        runtime = PluginRuntime()

        output_lines: list[str] = []
        input_queue: asyncio.Queue[str] = asyncio.Queue()

        runtime._reader = input_queue
        runtime._writer = self._make_writer(output_lines)

        input_queue.put_nowait("this is not valid json {{{")
        input_queue.put_nowait(
            json.dumps({"id": 1, "method": "plugin.health", "params": {}})
        )
        input_queue.put_nowait("")

        await runtime.run()

        # Should still get a health response
        responses = [json.loads(line) for line in output_lines if '"id"' in line]
        assert len(responses) >= 1
        assert responses[-1]["result"]["status"] == "ok"

    @pytest.mark.asyncio
    async def test_custom_handler_is_called(self):
        runtime = PluginRuntime()
        handler_called = []

        @runtime.on(METHOD_TOOL_EXECUTE)
        def handle_execute(params):
            handler_called.append(params)
            return {"success": True, "data": "handled"}

        output_lines: list[str] = []
        input_queue: asyncio.Queue[str] = asyncio.Queue()

        runtime._reader = input_queue
        runtime._writer = self._make_writer(output_lines)

        input_queue.put_nowait(
            json.dumps(
                {
                    "id": 3,
                    "method": METHOD_TOOL_EXECUTE,
                    "params": {"tool": "echo", "args": {"msg": "hello"}},
                }
            )
        )
        input_queue.put_nowait("")

        await runtime.run()

        assert len(handler_called) == 1
        assert handler_called[0]["tool"] == "echo"

        responses = [
            json.loads(line)
            for line in output_lines
            if '"id"' in line and line.strip().startswith('{"id": 3')
        ]
        assert len(responses) == 1
        assert responses[0]["result"] == {"success": True, "data": "handled"}

    @pytest.mark.asyncio
    async def test_default_tool_execute_dispatches_registered_tool(self):
        runtime = PluginRuntime()

        class EchoTool:
            name = "echo"
            description = "echo"
            parameters = {}

            async def execute(self, **kwargs):
                from courtier.agent.tools.protocol import ToolResult

                return ToolResult(success=True, data={"msg": kwargs.get("msg")})

        runtime.register_tool(EchoTool())

        output_lines: list[str] = []
        input_queue: asyncio.Queue[str] = asyncio.Queue()
        runtime._reader = input_queue
        runtime._writer = self._make_writer(output_lines)

        input_queue.put_nowait(
            json.dumps(
                {
                    "id": 1,
                    "method": METHOD_TOOL_EXECUTE,
                    "params": {"tool": "echo", "args": {"msg": "hi"}},
                }
            )
        )
        input_queue.put_nowait("")

        await runtime.run()

        responses = [
            json.loads(line)
            for line in output_lines
            if '"id"' in line and line.strip().startswith('{"id": 1')
        ]
        assert len(responses) == 1
        assert responses[0]["result"]["success"] is True
        assert responses[0]["result"]["data"] == {"msg": "hi"}


class TestPluginCancellation:
    """request.cancel notification must stop a running handler task."""

    @pytest.mark.asyncio
    async def test_request_cancel_stops_sync_tool_handler(self):
        runtime = PluginRuntime()
        output_lines: list[str] = []

        class TestWriter:
            def write(self, data: str | bytes):
                text = data.decode("utf-8") if isinstance(data, bytes) else data
                output_lines.append(text)

            def flush(self):
                pass

        runtime._writer = TestWriter()
        handler_started = threading.Event()

        @runtime.on(METHOD_TOOL_EXECUTE)
        def handle_execute(params):
            handler_started.set()
            time.sleep(10)
            return {"success": True, "data": "should not appear"}

        request_msg = {
            "id": 42,
            "method": METHOD_TOOL_EXECUTE,
            "params": {"tool": "slow", "args": {}},
        }
        task = asyncio.create_task(runtime._handle_request_async(request_msg))

        await asyncio.wait_for(asyncio.to_thread(handler_started.wait), timeout=5.0)

        runtime._handle_notification({"method": "request.cancel", "params": {"id": 42}})

        with pytest.raises(asyncio.CancelledError):
            await task

        id_42_responses = [
            json.loads(line)
            for line in output_lines
            if line.strip() and '"id": 42' in line and '"method"' not in line
        ]
        assert not any(
            r.get("result", {}).get("data") == "should not appear" for r in id_42_responses
        )


class TestPluginContextManager:
    """PluginContextManager delegates cache operations to the host."""

    @pytest.mark.asyncio
    async def test_persist_large_output_forwards_to_host(self):
        from courtier.plugin.sdk.context_manager import PluginContextManager
        from courtier.plugin.sdk.protocol import METHOD_CACHE_PERSIST

        calls: list[tuple[str, dict]] = []

        class MockHost:
            async def call(
                self, method: str, params: dict | None = None, timeout: float = 30.0
            ):
                calls.append((method, params or {}))
                if method == METHOD_CACHE_PERSIST:
                    return {
                        "data": {
                            "__persisted_output__": True,
                            "ref_id": "$ref:p.big:1",
                        },
                        "ref_id": "$ref:p.big:1",
                        "persisted": True,
                    }
                return None

        cm = PluginContextManager(MockHost(), plugin_name="p")
        result = await cm.persist_large_output("big", "x" * 5000)

        assert result == {"__persisted_output__": True, "ref_id": "$ref:p.big:1"}
        assert len(calls) == 1
        assert calls[0][0] == METHOD_CACHE_PERSIST
        assert calls[0][1]["tool_name"] == "p.big"

    @pytest.mark.asyncio
    async def test_resolve_refs_forwards_to_host(self):
        from courtier.plugin.sdk.context_manager import PluginContextManager
        from courtier.plugin.sdk.protocol import METHOD_CACHE_RESOLVE

        class MockHost:
            async def call(
                self, method: str, params: dict | None = None, timeout: float = 30.0
            ):
                if method == METHOD_CACHE_RESOLVE:
                    return {"doc": "loaded content"}
                return None

        cm = PluginContextManager(MockHost())
        resolved = await cm.resolve_refs({"doc": "$ref:load:1"})
        assert resolved == {"doc": "loaded content"}


class TestSanitizeRpcLog:
    def test_redacts_api_key_from_params(self):
        line = json.dumps(
            {
                "method": "tool.execute",
                "params": {"api_key": "sk-live-secret-abc123", "text": "hello"},
            }
        )
        sanitized = _sanitize_rpc_log(line)
        assert "sk-live-secret-abc123" not in sanitized
        assert "***" in sanitized
        assert "tool.execute" in sanitized

    def test_redacts_nested_secrets(self):
        line = json.dumps(
            {
                "method": "config.update",
                "params": {
                    "config": {"password": "p@ssw0rd", "username": "admin"},
                },
            }
        )
        sanitized = _sanitize_rpc_log(line)
        assert "p@ssw0rd" not in sanitized
        assert "***" in sanitized

    def test_returns_truncated_non_json_line(self):
        line = "a" * 500
        sanitized = _sanitize_rpc_log(line)
        assert len(sanitized) <= 200
