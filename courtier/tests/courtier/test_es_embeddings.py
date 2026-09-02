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
        llm_embedding_base_url="https://example.com/v1",
        llm_embedding_api_key="k",
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


class TestIndependentEndpoint:
    """embedding 与聊天 LLM 端点彻底解耦：专用端点未设置 = 向量检索关闭，
    不回退 llm_base_url / llm_api_key。"""

    def _settings(self, **overrides) -> SimpleNamespace:
        base = dict(
            llm_base_url="https://chat.example.com/v1",
            llm_api_key="sk-chat",
            llm_embedding_model="text-embedding-v3",
            llm_embedding_batch_size=2,
            llm_embedding_base_url="",
            llm_embedding_api_key="",
        )
        base.update(overrides)
        return SimpleNamespace(**base)

    @pytest.mark.asyncio
    async def test_unset_endpoint_disables_vector_retrieval(self, monkeypatch):
        # 标量端点/密钥即使配置齐全，专用端点未设置也一律关闭
        settings = self._settings()
        assert embedding_enabled(settings) is False
        vectors = await embed_chunks(settings, ["甲", "乙"])
        assert vectors == [None, None]

    @pytest.mark.asyncio
    async def test_dedicated_url_enables_and_uses_dedicated_key(self, monkeypatch):
        class RecordingClient(_FakeAsyncClient):
            last_headers = None

            async def post(self, url, json=None, headers=None):
                type(self).last_headers = headers
                return await super().post(url, json=json, headers=headers)

        _FakeAsyncClient.calls.clear()
        monkeypatch.setattr(httpx, "AsyncClient", RecordingClient)
        settings = self._settings(
            llm_embedding_base_url="https://emb.example.com/v1",
            llm_embedding_api_key="sk-emb",
        )
        await embed_chunks(settings, ["甲"])

        url, _payload = _FakeAsyncClient.calls[0]
        assert url == "https://emb.example.com/v1/embeddings"
        assert RecordingClient.last_headers["Authorization"] == "Bearer sk-emb"

    @pytest.mark.asyncio
    async def test_dedicated_key_optional(self, monkeypatch):
        # 端点不需要鉴权时可不填密钥：不发 Authorization 头
        class RecordingClient(_FakeAsyncClient):
            last_headers = None

            async def post(self, url, json=None, headers=None):
                type(self).last_headers = headers
                return await super().post(url, json=json, headers=headers)

        _FakeAsyncClient.calls.clear()
        monkeypatch.setattr(httpx, "AsyncClient", RecordingClient)
        settings = self._settings(llm_embedding_base_url="https://emb.example.com/v1")
        await embed_chunks(settings, ["甲"])

        assert "Authorization" not in (RecordingClient.last_headers or {})
        assert embedding_enabled(settings) is True

    def test_scalar_llm_key_never_leaks_into_embedding(self):
        # 聊天密钥存在与否不影响 embedding 的鉴权来源
        settings = self._settings(
            llm_embedding_base_url="https://emb.example.com/v1", llm_embedding_api_key=""
        )
        from courtier.es.embeddings import _effective_api_key

        assert _effective_api_key(settings) == ""

    def test_legacy_settings_shape_is_disabled(self):
        # 无 llm_embedding_base_url 属性的旧 mock 配置 = 关闭（新契约）
        legacy = SimpleNamespace(
            llm_base_url="https://legacy/v1",
            llm_api_key="k",
            llm_embedding_model="emb",
        )
        assert embedding_enabled(legacy) is False
