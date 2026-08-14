"""Tests for the host-side embedding client used by resource ingestion."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from courtier.es.embeddings import embed_chunks, embedding_enabled


class _FakeResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class _FakeAsyncClient:
    calls: list[tuple[str, dict]] = []

    def __init__(self, timeout=None):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, json=None, headers=None):
        type(self).calls.append((url, json))
        texts = json["input"] if isinstance(json["input"], list) else [json["input"]]
        return _FakeResponse(
            {"data": [{"index": i, "embedding": [float(i), 0.5]} for i in range(len(texts))]}
        )


def _settings(enabled: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        llm_base_url="https://example.com/v1",
        llm_api_key="k",
        llm_embedding_model="text-embedding-v3" if enabled else "",
        llm_embedding_batch_size=2,
    )


@pytest.mark.asyncio
async def test_embedding_disabled_returns_none_entries():
    assert embedding_enabled(_settings(enabled=False)) is False
    vectors = await embed_chunks(_settings(enabled=False), ["甲", "乙"])
    assert vectors == [None, None]


@pytest.mark.asyncio
async def test_embedding_happy_path_batches(monkeypatch):
    _FakeAsyncClient.calls.clear()
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    vectors = await embed_chunks(_settings(), ["甲", "乙", "丙"])
    assert vectors == [[0.0, 0.5], [1.0, 0.5], [0.0, 0.5]]
    assert len(_FakeAsyncClient.calls) == 2  # batched 2 + 1
    for _url, payload in _FakeAsyncClient.calls:
        assert payload["model"] == "text-embedding-v3"


@pytest.mark.asyncio
async def test_embedding_failure_degrades_to_none(monkeypatch):
    class FailingClient(_FakeAsyncClient):
        async def post(self, url, json=None, headers=None):
            raise RuntimeError("boom")

    monkeypatch.setattr(httpx, "AsyncClient", FailingClient)
    vectors = await embed_chunks(_settings(), ["甲", "乙"])
    assert vectors == [None, None]
