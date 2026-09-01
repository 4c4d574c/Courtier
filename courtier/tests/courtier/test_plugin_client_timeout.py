"""Host-side JSONRPCClient timeout error message tests.

A bare ``asyncio.TimeoutError`` stringifies to "" — the model would see an
empty ``error`` field.  The client must re-raise with an actionable Chinese
message carrying the plugin name and the timeout duration.
"""

from __future__ import annotations

import asyncio

import pytest

from courtier.plugin.client import JSONRPCClient


class _NeverEndingReader:
    """A reader whose plugin never answers within the test's patience."""

    async def readline(self) -> bytes:
        await asyncio.sleep(3600)
        return b""


class _FakeWriter:
    def write(self, data: bytes) -> None:
        pass

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass


@pytest.mark.asyncio
async def test_call_timeout_raises_actionable_chinese_message():
    client = JSONRPCClient(_NeverEndingReader(), _FakeWriter(), "parse", default_timeout=120.0)
    try:
        with pytest.raises(asyncio.TimeoutError) as exc_info:
            await client.call("tool.execute", {"tool": "parse_layout"}, timeout=0.05)
        message = str(exc_info.value)
        assert message  # never the empty string
        assert "插件 'parse' 调用超时" in message
        assert "0.05" in message
        assert "请重试或拆分任务" in message
    finally:
        client.close()


@pytest.mark.asyncio
async def test_call_timeout_message_formats_seconds():
    client = JSONRPCClient(_NeverEndingReader(), _FakeWriter(), "parse", default_timeout=120.0)
    try:
        with pytest.raises(asyncio.TimeoutError) as exc_info:
            await client.call("tool.execute", {"tool": "parse_layout"}, timeout=0.01)
        # Fractional timeouts render as-is; integral floats render without ".0".
        assert "（0.01s）" in str(exc_info.value)
    finally:
        client.close()
