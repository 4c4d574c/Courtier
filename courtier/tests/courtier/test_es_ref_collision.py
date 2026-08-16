"""Ref-id collision prevention: ES-seeded numbering + cross-process load."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from courtier.agent.artifacts.store import ArtifactStore
from courtier.agent.runtime.es_backend import (
    _seq_seed_cache,
)


def _fake_es_client(existing: dict[str, int] | None = None):
    """In-memory stand-in for the ES client used by ElasticsearchResultBackend."""
    docs: dict[str, dict] = {
        rid: {"data": data} for rid, data in (existing or {}).items()
    }

    client = MagicMock()

    def _search(index=None, body=None, **kw):
        # Aggregate max seq per tool over stored docs.
        maxima: dict[str, int] = {}
        for rid in docs:
            if not rid.startswith("$ref:"):
                continue
            tool, _, seq = rid.lstrip("$ref:").rpartition(":")
            if seq.isdigit():
                maxima[tool] = max(maxima.get(tool, 0), int(seq))
        return {
            "aggregations": {
                "by_tool": {
                    "buckets": [
                        {"key": tool, "max_seq": {"value": seq}}
                        for tool, seq in maxima.items()
                    ]
                }
            }
        }

    def _get(index=None, id=None, **kw):
        if id in docs:
            return {"found": True, "_source": docs[id]}
        raise Exception("404")

    def _index(index=None, id=None, body=None, **kw):
        docs[id] = body

    client.search.side_effect = _search
    client.get.side_effect = _get
    client.index.side_effect = _index
    client.indices.create.side_effect = Exception("resource_already_exists_exception")
    client._docs = docs
    return client


def _backend(client, monkeypatch, index_name="courtier_results"):
    from courtier.agent.runtime import es_backend as mod

    monkeypatch.setattr(mod, "get_es_client", lambda: client)
    from courtier.agent.runtime.es_backend import ElasticsearchResultBackend

    return ElasticsearchResultBackend(index_name=index_name)


@pytest.fixture(autouse=True)
def _clear_seed_cache():
    from courtier.agent.core import cache_store as _cs

    _seq_seed_cache.clear()
    _cs._SHARED_REF_COUNTERS.clear()
    yield
    _seq_seed_cache.clear()
    _cs._SHARED_REF_COUNTERS.clear()


class TestRefSequenceSeeding:
    @pytest.mark.asyncio
    async def test_fresh_store_continues_past_index_sequences(self, tmp_path, monkeypatch):
        client = _fake_es_client()
        backend = _backend(client, monkeypatch)
        # Pre-existing documents from a "previous session".
        client.index(index="courtier_results", id="$ref:search_documents:7", body={"data": {}})

        store = ArtifactStore(cache_dir=str(tmp_path), primary_backend=backend)
        result = await store.persist({"hits": ["x"]}, "search_documents", force=True)
        assert result.ref_id == "$ref:search_documents:8"

    @pytest.mark.asyncio
    async def test_two_stores_never_collide(self, tmp_path, monkeypatch):
        client = _fake_es_client()
        backend = _backend(client, monkeypatch)
        s1 = ArtifactStore(cache_dir=str(tmp_path / "a"), primary_backend=backend)
        s2 = ArtifactStore(cache_dir=str(tmp_path / "b"), primary_backend=backend)
        r1 = await s1.persist({"a": 1}, "search_documents", force=True)
        r2 = await s2.persist({"b": 2}, "search_documents", force=True)
        assert r1.ref_id != r2.ref_id, (
            f"seed={client.search.call_count}, docs={list(client._docs)}, "
            f"cache={_seq_seed_cache}"
        )
        assert r1.ref_id in client._docs and r2.ref_id in client._docs

    def test_seed_cache_hits_within_ttl(self, monkeypatch):
        client = _fake_es_client()
        backend = _backend(client, monkeypatch)
        client.search.reset_mock()
        backend.max_ref_sequences()
        backend.max_ref_sequences()
        assert client.search.call_count == 1

    def test_es_unreachable_seeding_is_skipped(self, tmp_path, monkeypatch):
        client = _fake_es_client()
        client.search.side_effect = ConnectionError("down")
        backend = _backend(client, monkeypatch)
        store = ArtifactStore(cache_dir=str(tmp_path), primary_backend=backend)
        # Numbering falls back to local counters — disk files still get the
        # uniqueness suffix, so functionality does not degrade.
        assert asyncio.run(store.persist({"x": 1}, "echo", force=True)).ref_id == "$ref:echo:1"


class TestLoadEsFallback:
    @pytest.mark.asyncio
    async def test_load_reads_ref_written_by_another_process(self, tmp_path, monkeypatch):
        """The sess_8146 shape: a new process loads refs written by the
        session that persisted them (disk has no record)."""
        client = _fake_es_client()
        backend = _backend(client, monkeypatch)
        writer = ArtifactStore(cache_dir=str(tmp_path / "writer"), primary_backend=backend)
        result = await writer.persist(
            {"hits": [{"title": "安全生产法"}]}, "search_documents", force=True
        )
        assert result.ref_id in client._docs

        reader = ArtifactStore(cache_dir=str(tmp_path / "reader"), primary_backend=backend)
        data = reader.load(result.ref_id)
        assert data == {"hits": [{"title": "安全生产法"}]}

    def test_load_returns_none_when_missing_everywhere(self, tmp_path, monkeypatch):
        client = _fake_es_client()
        backend = _backend(client, monkeypatch)
        store = ArtifactStore(cache_dir=str(tmp_path), primary_backend=backend)
        assert store.load("$ref:search_documents:99") is None


class TestReadPrefersLocalDisk:
    @pytest.mark.asyncio
    async def test_read_returns_local_write_not_stale_es_doc(self, tmp_path, monkeypatch):
        """Regression for sess_8873fbece988: another session's document sat
        in ES under the same ref id; read() asked ES first and returned the
        wrong session's content."""
        client = _fake_es_client()
        backend = _backend(client, monkeypatch)
        # A "previous session" already owns $ref:content_audit:1 in ES, so
        # the fresh store numbers past it.
        client._docs["$ref:content_audit:1"] = {
            "data": "旧会话的安全生产报告",
            "data_text": "旧会话的安全生产报告",
        }

        store = ArtifactStore(cache_dir=str(tmp_path), primary_backend=backend)
        result = await store.persist("本会话的智慧城市报告", "content_audit", force=True)
        assert result.ref_id == "$ref:content_audit:2"

        # ES write conflicts (or a lagging index) can leave a stale document
        # under this session's id — reads must still prefer the local disk.
        client._docs["$ref:content_audit:2"] = {
            "data": "ES 里的陈旧内容",
            "data_text": "ES 里的陈旧内容",
        }
        read = await store.read("$ref:content_audit:2")
        assert read.get("data") == "本会话的智慧城市报告"

    @pytest.mark.asyncio
    async def test_seed_numbers_past_legacy_docs_without_tool_seq(self, tmp_path, monkeypatch):
        """Legacy ES docs carry only result_id; seeding must still count them."""
        client = _fake_es_client()
        backend = _backend(client, monkeypatch)
        client._docs["$ref:content_audit:1"] = {"data": "旧"}
        client._docs["$ref:search_documents:3"] = {"data": "旧"}

        store = ArtifactStore(cache_dir=str(tmp_path), primary_backend=backend)
        result = await store.persist("新数据", "content_audit", force=True)
        assert result.ref_id == "$ref:content_audit:2"
