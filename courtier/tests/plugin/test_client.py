"""Tests for JSONRPCClient."""

import asyncio
import json

import pytest

from courtier.plugin.client import JSONRPCClient, PluginCrashedError, PluginRPCError


class PipeStreams:
    """Simulates stdin/stdout pair for testing JSONRPCClient without a real subprocess."""

    def __init__(self):
        self._read_queue: asyncio.Queue[str] = asyncio.Queue()
        self._written: list[str] = []

    async def readline(self) -> bytes:
        line = await self._read_queue.get()
        return line.encode("utf-8")

    def write(self, data: bytes) -> None:
        self._written.append(data.decode("utf-8"))

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass

    def feed_line(self, line: str) -> None:
        self._read_queue.put_nowait(line)

    @property
    def written(self) -> list[dict]:
        return [json.loads(line) for line in self._written]


class TestJSONRPCClient:
    @pytest.fixture
    def streams(self):
        return PipeStreams()

    @pytest.fixture
    def client(self, streams):
        return JSONRPCClient(streams, streams, plugin_name="test_plugin")

    @pytest.mark.asyncio
    async def test_call_sends_request_and_receives_response(self, client, streams):
        # Feed the response before awaiting
        streams.feed_line(json.dumps({"id": 1, "result": {"success": True, "data": "hello"}}))

        result = await client.call("echo", {"message": "hello"})
        assert result == {"success": True, "data": "hello"}

        # Verify the request was written
        assert len(streams.written) == 1
        req = streams.written[0]
        assert req["id"] == 1
        assert req["method"] == "echo"
        assert req["params"] == {"message": "hello"}

    @pytest.mark.asyncio
    async def test_call_receives_error_response(self, client, streams):
        streams.feed_line(
            json.dumps(
                {
                    "id": 1,
                    "error": {"code": -32000, "message": "Tool not found"},
                }
            )
        )

        with pytest.raises(PluginRPCError, match="Tool not found"):
            await client.call("bad_tool", {})

    @pytest.mark.asyncio
    async def test_call_timeout(self, client, streams):
        with pytest.raises(asyncio.TimeoutError):
            await client.call("slow_tool", {}, timeout=0.01)

    @pytest.mark.asyncio
    async def test_request_ids_are_sequential(self, client, streams):
        streams.feed_line(json.dumps({"id": 1, "result": "first"}))
        streams.feed_line(json.dumps({"id": 2, "result": "second"}))

        r1 = await client.call("method1", {})
        r2 = await client.call("method2", {})
        assert r1 == "first"
        assert r2 == "second"
        assert streams.written[0]["id"] == 1
        assert streams.written[1]["id"] == 2

    @pytest.mark.asyncio
    async def test_notify_writes_notification_without_id(self, client, streams):
        await client.notify("plugin.shutdown", {"reason": "test"})

        assert len(streams.written) == 1
        notif = streams.written[0]
        assert "id" not in notif
        assert notif["method"] == "plugin.shutdown"
        assert notif["params"] == {"reason": "test"}

    @pytest.mark.asyncio
    async def test_wait_for_register_receives_capabilities(self, client, streams):
        streams.feed_line(
            json.dumps(
                {
                    "method": "plugin.register",
                    "params": {
                        "capabilities": [
                            {"type": "tool", "name": "echo"},
                            {"type": "checker", "doc_type": "通知"},
                        ]
                    },
                }
            )
        )

        caps = await client.wait_for_register(timeout=1.0)
        assert len(caps) == 2
        assert caps[0] == {"type": "tool", "name": "echo"}
        assert caps[1] == {"type": "checker", "doc_type": "通知"}

    @pytest.mark.asyncio
    async def test_wait_for_register_timeout(self, client, streams):
        with pytest.raises(asyncio.TimeoutError):
            await client.wait_for_register(timeout=0.01)

    @pytest.mark.asyncio
    async def test_close_cleans_up(self, client, streams):
        client.close()
        assert client._closed is True
        assert client._reader_task is None
        assert len(client._pending) == 0

    @pytest.mark.asyncio
    async def test_call_on_closed_client_raises(self, client, streams):
        client.close()
        with pytest.raises(PluginCrashedError):
            await client.call("some_method", {})

    @pytest.mark.asyncio
    async def test_disconnect_triggers_on_disconnect_callback(self, streams):
        """When EOF is received, on_disconnect should be called."""
        import asyncio

        disconnect_called = asyncio.Event()

        async def on_disconnect():
            disconnect_called.set()

        client = JSONRPCClient(
            streams,
            streams,
            plugin_name="test_plugin",
            on_disconnect=on_disconnect,
        )

        # Start the reader
        await client._ensure_reader()
        await asyncio.sleep(0.02)  # Let reader task start

        # Feed EOF — reader task will detect it
        streams.feed_line("")
        await asyncio.wait_for(disconnect_called.wait(), timeout=1.0)

        assert disconnect_called.is_set()
        assert client._closed is False  # on_disconnect doesn't close; manager does that

    @pytest.mark.asyncio
    async def test_plugin_to_host_request_invokes_handler(self, client, streams):
        """Messages with both id and method are routed to the host handler."""
        handled = asyncio.Event()
        received: dict = {}

        async def handler(request: dict) -> dict:
            received.update(request)
            handled.set()
            return {"status": "ok"}

        client.set_host_request_handler(handler)
        await client._ensure_reader()
        await asyncio.sleep(0.02)

        streams.feed_line(
            json.dumps(
                {
                    "id": -1,
                    "method": "cache.load",
                    "params": {"ref_id": "$ref:x:1"},
                }
            )
        )

        await asyncio.wait_for(handled.wait(), timeout=1.0)
        assert received.get("method") == "cache.load"
        assert received.get("params", {}).get("ref_id") == "$ref:x:1"

        for _ in range(100):
            for msg in streams.written:
                if msg.get("id") == -1:
                    assert msg.get("result") == {"status": "ok"}
                    return
            await asyncio.sleep(0.01)
        pytest.fail("Host response was not written")

    @pytest.mark.asyncio
    async def test_plugin_to_host_request_without_handler_returns_error(self, client, streams):
        """If no host handler is set, plugin requests receive a METHOD_NOT_FOUND error."""
        await client._ensure_reader()
        await asyncio.sleep(0.02)

        streams.feed_line(
            json.dumps(
                {
                    "id": -2,
                    "method": "cache.load",
                    "params": {},
                }
            )
        )

        for _ in range(100):
            for msg in streams.written:
                if msg.get("id") == -2:
                    assert "error" in msg
                    assert msg["error"]["code"] == -32601
                    return
            await asyncio.sleep(0.01)
        pytest.fail("No error response was written")


class TestLargeResponses:
    @pytest.mark.asyncio
    async def test_response_larger_than_64kib_roundtrips(self):
        """Regression: a single-line response above the old 64 KiB default
        stream limit must not kill the connection.  It used to break the
        read loop and make the host kill perfectly healthy plugins.
        """
        import sys

        from courtier_plugin_sdk.protocol import STREAM_LIMIT_BYTES

        big = "x" * 200_000
        # The payload is generated inside the child — embedding it in argv
        # trips E2BIG (argument list too long).
        script = (
            "import sys, json\n"
            "sys.stdin.buffer.readline()\n"
            "resp = json.dumps({'id': 1, 'result': {'success': True, 'data': 'x' * 200000}})\n"
            "sys.stdout.buffer.write(resp.encode() + b'\\n')\n"
            "sys.stdout.buffer.flush()\n"
        )
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            script,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            limit=STREAM_LIMIT_BYTES,
        )
        client = JSONRPCClient(proc.stdout, proc.stdin, plugin_name="big")
        try:
            result = await client.call("echo", {})
            assert result == {"success": True, "data": big}
        finally:
            proc.kill()
            await proc.wait()

    @pytest.mark.asyncio
    async def test_default_limit_would_break(self):
        """Document the failure mode: with the asyncio default 64 KiB limit,
        an oversized line raises ValueError from readline."""
        import sys

        script = (
            "import sys, json\n"
            "sys.stdin.buffer.readline()\n"
            "resp = json.dumps({'id': 1, 'result': {'data': 'x' * 200000}})\n"
            "sys.stdout.buffer.write(resp.encode() + b'\\n')\n"
            "sys.stdout.buffer.flush()\n"
        )
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            script,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            # no limit= → 64 KiB default
        )
        proc.stdin.write(b'{"id": 1, "method": "echo"}\n')
        await proc.stdin.drain()
        with pytest.raises(ValueError):
            await proc.stdout.readline()
        proc.kill()
        await proc.wait()
