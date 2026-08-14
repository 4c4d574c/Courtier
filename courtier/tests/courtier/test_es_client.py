"""Tests for the versioned chunks index / alias write-path routing."""

from __future__ import annotations

import pytest

import courtier.es.client as es_client


class FakeIndices:
    def __init__(self, alias_map=None, plain_indices=None):
        self._alias_map = alias_map or {}
        self._plain = set(plain_indices or [])

    def exists_alias(self, name):
        return name in self._alias_map

    def get_alias(self, name):
        return {real: {} for real in self._alias_map.get(name, [])}

    def exists(self, index):
        # An alias resolves as existing too, matching ES semantics.
        return index in self._plain or index in self._alias_map


class FakeES:
    def __init__(self, indices):
        self.indices = indices


@pytest.fixture(autouse=True)
def _reset_cache():
    es_client._invalidate_write_index_cache()
    yield
    es_client._invalidate_write_index_cache()


def _install(monkeypatch, indices, base="courtier_chunks"):
    monkeypatch.setattr(es_client, "_es_index_name", lambda: base)
    monkeypatch.setattr(es_client, "get_es_client", lambda: FakeES(indices))
    return indices


def test_resolve_uses_alias_backing_index(monkeypatch):
    indices = FakeIndices(alias_map={"courtier_chunks": ["courtier_chunks_v2"]})
    _install(monkeypatch, indices)
    assert es_client.resolve_write_index() == "courtier_chunks_v2"


def test_resolve_legacy_plain_index(monkeypatch):
    indices = FakeIndices(plain_indices={"courtier_chunks"})
    _install(monkeypatch, indices)
    assert es_client.resolve_write_index() == "courtier_chunks"


def test_resolve_defaults_to_v1_when_missing(monkeypatch):
    _install(monkeypatch, FakeIndices())
    assert es_client.resolve_write_index() == "courtier_chunks_v1"


def test_resolve_caches_for_ttl(monkeypatch):
    indices = FakeIndices(alias_map={"courtier_chunks": ["courtier_chunks_v1"]})
    _install(monkeypatch, indices)
    assert es_client.resolve_write_index() == "courtier_chunks_v1"
    # Swap behind the cache's back — cached value must persist within the TTL.
    indices._alias_map = {"courtier_chunks": ["courtier_chunks_v2"]}
    assert es_client.resolve_write_index() == "courtier_chunks_v1"
    es_client._write_index_cache_ts -= es_client._WRITE_INDEX_TTL_SECONDS + 1
    assert es_client.resolve_write_index() == "courtier_chunks_v2"


def test_rewrite_bulk_indices_handles_all_meta_keys():
    actions = [
        {"index": {"_index": "old", "_id": "1"}},
        {"chunk_text": "a"},
        {"create": {"_index": "old", "_id": "2"}},
        {"chunk_text": "b"},
        {"update": {"_index": "old", "_id": "3"}},
        {"doc": {}},
        {"delete": {"_index": "old", "_id": "4"}},
    ]
    es_client._rewrite_bulk_indices(actions, "new")
    assert actions[0]["index"]["_index"] == "new"
    assert actions[2]["create"]["_index"] == "new"
    assert actions[4]["update"]["_index"] == "new"
    assert actions[6]["delete"]["_index"] == "new"
    # Body dicts are left untouched.
    assert actions[1] == {"chunk_text": "a"}
    assert actions[3] == {"chunk_text": "b"}


def test_mapping_uses_bigram_analyzer_on_text_fields():
    mapping = es_client.INDEX_MAPPING
    assert mapping["mappings"]["properties"]["chunk_text"]["analyzer"] == "cjk"
    assert mapping["mappings"]["properties"]["title"]["analyzer"] == "cjk"
    # Keyword fields keep their types.
    assert mapping["mappings"]["properties"]["doc_type"] == {"type": "keyword"}


def test_mapping_dense_vector_dims_configurable():
    mapping = es_client.build_index_mapping(embedding_dim=768)
    assert mapping["mappings"]["properties"]["chunk_vector"] == {
        "type": "dense_vector",
        "dims": 768,
        "index": True,
        "similarity": "cosine",
    }
    assert es_client.INDEX_MAPPING["mappings"]["properties"]["chunk_vector"]["dims"] == 1024
