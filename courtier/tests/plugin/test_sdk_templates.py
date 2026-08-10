"""Tests for the plugin-side HostTemplateStore client."""

from __future__ import annotations

import pytest
from courtier_plugin_sdk.protocol import METHOD_TEMPLATE_STORE_GET
from courtier_plugin_sdk.templates import HostTemplateStore


class _FakeHostClient:
    def __init__(self, result):
        self._result = result
        self.calls: list[tuple[str, dict]] = []

    async def call(self, method, params=None, timeout=30.0):
        self.calls.append((method, params or {}))
        return self._result


@pytest.mark.asyncio
async def test_get_by_doc_type_sends_rpc_and_returns_template():
    template = {"version": 1, "page": {}, "elements": []}
    client = _FakeHostClient(template)
    store = HostTemplateStore(client)

    result = await store.get("通知")

    assert result == template
    method, params = client.calls[0]
    assert method == METHOD_TEMPLATE_STORE_GET
    assert params == {"doc_type": "通知"}


@pytest.mark.asyncio
async def test_get_with_template_id_includes_id_param():
    client = _FakeHostClient(None)
    store = HostTemplateStore(client)

    result = await store.get("通知", template_id=7)

    assert result is None
    _, params = client.calls[0]
    assert params == {"doc_type": "通知", "template_id": 7}
