"""Tests for the plugin-side HostStorage client."""

from __future__ import annotations

import base64

import pytest
from courtier_plugin_sdk.protocol import METHOD_STORAGE_PUT
from courtier_plugin_sdk.storage import HostStorage


class _FakeHostClient:
    def __init__(self, result):
        self._result = result
        self.calls: list[tuple[str, dict]] = []

    async def call(self, method, params=None, timeout=30.0):
        self.calls.append((method, params or {}))
        return self._result


@pytest.mark.asyncio
async def test_put_sends_base64_payload_and_returns_receipt():
    receipt = {
        "bucket": "courtier-docs",
        "object_key": "plugin-outputs/abc/a.docx",
        "download_url": "http://minio/courtier-docs/plugin-outputs/abc/a.docx",
        "expires_in": 604800,
        "size_bytes": 3,
    }
    client = _FakeHostClient(receipt)
    storage = HostStorage(client)

    result = await storage.put("a.docx", b"\x89abc", "application/octet-stream")

    assert result == receipt
    method, params = client.calls[0]
    assert method == METHOD_STORAGE_PUT
    assert params["filename"] == "a.docx"
    assert base64.b64decode(params["data_b64"]) == b"\x89abc"
    assert params["content_type"] == "application/octet-stream"


@pytest.mark.asyncio
async def test_put_default_content_type():
    client = _FakeHostClient({})
    storage = HostStorage(client)

    await storage.put("a.bin", b"x")

    _, params = client.calls[0]
    assert params["content_type"] == "application/octet-stream"
