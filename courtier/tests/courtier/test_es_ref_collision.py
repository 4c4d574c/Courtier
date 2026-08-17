"""Session-scoped ref isolation: ES documents and per-session numbering.

Ref ids (``$ref:<tool>:N``) are session-local — each session numbers its
own results from 1, ES documents are keyed by ``{session_id}#{ref}``, and
one session can never read or overwrite another's results.
"""

from __future__ import annotations

import glob
from unittest.mock import MagicMock

import pytest

from courtier.agent.artifacts.store import ArtifactStore


def _fake_es_client():
    """In-memory stand-in for the ES client used by ElasticsearchResultBackend."""
    docs: dict[str, dict] = {}

    client = MagicMock()

    def _get(index=None, id=None, **kw):
        if id in docs:
            return {"found": True, "_source": docs[id]}
        raise Exception("404")

    def _exists(index=None, id=None, **kw):
        return id in docs

    def _index(index=None, id=None, body=None, op_type=None, **kw):
        if op_type == "create" and id in docs:
            raise Exception("version_conflict_engine_exception")
        docs[id] = body

    client.get.side_effect = _get
    client.exists.side_effect = _exists
    client.index.side_effect = _index
    client.indices.create.side_effect = Exception("resource_already_exists_exception")
    client.indices.put_mapping = MagicMock()
    client._docs = docs
    return client


def _backend(client, monkeypatch, index_name="courtier_results", session_id=""):
    from courtier.agent.runtime import es_backend as mod

    monkeypatch.setattr(mod, "get_es_client", lambda: client)
    from courtier.agent.runtime.es_backend import ElasticsearchResultBackend

    return ElasticsearchResultBackend(index_name=index_name, session_id=session_id)


class TestSessionScopedEsDocuments:
    @pytest.mark.asyncio
    async def test_store_writes_composite_id_and_session_field(self, tmp_path, monkeypatch):
        client = _fake_es_client()
        backend = _backend(client, monkeypatch, session_id="sess_a")
        store = ArtifactStore(
            cache_dir=str(tmp_path), primary_backend=backend, session_id="sess_a"
        )
        result = await store.persist({"hits": ["x"]}, "search_documents", force=True)

        assert result.ref_id == "$ref:search_documents:1"
        assert "sess_a#$ref:search_documents:1" in client._docs
        doc = client._docs["sess_a#$ref:search_documents:1"]
        assert doc["session_id"] == "sess_a"
        assert doc["result_id"] == "$ref:search_documents:1"
        # tool/seq fields no longer written (global numbering removed)
        assert "tool" not in doc
        assert "seq" not in doc

    @pytest.mark.asyncio
    async def test_same_ref_coexists_across_sessions(self, tmp_path, monkeypatch):
        client = _fake_es_client()
        b1 = _backend(client, monkeypatch, session_id="sess_a")
        b2 = _backend(client, monkeypatch, session_id="sess_b")
        s1 = ArtifactStore(cache_dir=str(tmp_path / "a"), primary_backend=b1, session_id="sess_a")
        s2 = ArtifactStore(cache_dir=str(tmp_path / "b"), primary_backend=b2, session_id="sess_b")

        r1 = await s1.persist({"who": "a"}, "search_documents", force=True)
        r2 = await s2.persist({"who": "b"}, "search_documents", force=True)
        # Both sessions number from 1 — no cross-session collision.
        assert r1.ref_id == r2.ref_id == "$ref:search_documents:1"
        assert len(client._docs) == 2
        assert client._docs["sess_a#$ref:search_documents:1"]["data"] == {"who": "a"}
        assert client._docs["sess_b#$ref:search_documents:1"]["data"] == {"who": "b"}

    @pytest.mark.asyncio
    async def test_read_only_hits_own_session(self, tmp_path, monkeypatch):
        client = _fake_es_client()
        b1 = _backend(client, monkeypatch, session_id="sess_a")
        b2 = _backend(client, monkeypatch, session_id="sess_b")
        s1 = ArtifactStore(cache_dir=str(tmp_path / "a"), primary_backend=b1, session_id="sess_a")
        s2 = ArtifactStore(cache_dir=str(tmp_path / "b"), primary_backend=b2, session_id="sess_b")

        r1 = await s1.persist({"secret": "a"}, "search_documents", force=True)
        assert r1.ref_id == "$ref:search_documents:1"

        # Session B cannot load session A's document by the same ref id.
        assert s2.load(r1.ref_id) is None
        raw = await s2.read(r1.ref_id)
        assert "error" in raw
        # Session A reads its own document fine.
        assert s1.load(r1.ref_id) == {"secret": "a"}

    @pytest.mark.asyncio
    async def test_duplicate_write_within_session_fails_loudly(self, tmp_path, monkeypatch):
        client = _fake_es_client()
        backend = _backend(client, monkeypatch, session_id="sess_a")
        store = ArtifactStore(
            cache_dir=str(tmp_path), primary_backend=backend, session_id="sess_a"
        )
        result = await store.persist({"x": 1}, "search_documents", force=True)
        # Direct re-store of the same composite id must conflict (create).
        with pytest.raises(Exception, match="version_conflict"):
            await backend.store(result.ref_id, {"x": 2})

    @pytest.mark.asyncio
    async def test_ensure_index_backfills_session_mapping(self, tmp_path, monkeypatch):
        client = _fake_es_client()
        backend = _backend(client, monkeypatch, session_id="sess_a")
        store = ArtifactStore(
            cache_dir=str(tmp_path), primary_backend=backend, session_id="sess_a"
        )
        # _ensure_index runs during persist; index already exists (fake
        # raises resource_already_exists on create) — put_mapping must add
        # the session_id keyword field.
        await store.persist({"x": 1}, "echo", force=True)
        client.indices.put_mapping.assert_called_once()
        _, kwargs = client.indices.put_mapping.call_args
        assert kwargs["body"]["properties"]["session_id"]["type"] == "keyword"


class TestPerSessionNumbering:
    @pytest.mark.asyncio
    async def test_two_sessions_number_from_one_independently(self, tmp_path):
        s1 = ArtifactStore(cache_dir=str(tmp_path / "a"), session_id="sess_a")
        s2 = ArtifactStore(cache_dir=str(tmp_path / "b"), session_id="sess_b")
        r1 = await s1.persist({"a": 1}, "echo", force=True)
        r2 = await s2.persist({"b": 2}, "echo", force=True)
        assert r1.ref_id == "$ref:echo:1"
        assert r2.ref_id == "$ref:echo:1"

    @pytest.mark.asyncio
    async def test_snapshot_roundtrip_continues_numbering(self, tmp_path):
        store = ArtifactStore(cache_dir=str(tmp_path), session_id="sess_a")
        await store.persist({"x": 1}, "echo", force=True)
        snapshot = store.snapshot()
        assert snapshot["ref_counters"] == {"echo": 1}

        restored = ArtifactStore.restore(snapshot, cache_dir=str(tmp_path), session_id="sess_a")
        r = await restored.persist({"x": 2}, "echo", force=True)
        assert r.ref_id == "$ref:echo:2"

    @pytest.mark.asyncio
    async def test_concurrent_persist_no_duplicate_seq(self, tmp_path):
        import asyncio

        store = ArtifactStore(cache_dir=str(tmp_path), session_id="sess_a")
        results = await asyncio.gather(
            *[store.persist({"i": i}, "echo", force=True) for i in range(20)]
        )
        seqs = sorted(int(r.ref_id.rsplit(":", 1)[1]) for r in results)
        assert seqs == list(range(1, 21))

    @pytest.mark.asyncio
    async def test_no_session_id_keeps_shared_directory(self, tmp_path):
        store = ArtifactStore(cache_dir=str(tmp_path))
        result = await store.persist({"x": 1}, "echo", force=True)
        assert result.ref_id == "$ref:echo:1"
        files = list(tmp_path.rglob("echo_1_*.json"))
        # File lives directly under cache_dir (no session subdir).
        assert files and files[0].parent == tmp_path

    @pytest.mark.asyncio
    async def test_session_directory_isolation(self, tmp_path):
        s1 = ArtifactStore(cache_dir=str(tmp_path), session_id="sess_a")
        s2 = ArtifactStore(cache_dir=str(tmp_path), session_id="sess_b")
        r1 = await s1.persist({"a": 1}, "echo", force=True)
        r2 = await s2.persist({"b": 2}, "echo", force=True)
        assert r1.ref_id == r2.ref_id == "$ref:echo:1"
        assert glob.glob(str(tmp_path / "sess_a" / "echo_1_*.json"))
        assert glob.glob(str(tmp_path / "sess_b" / "echo_1_*.json"))
        # Each session reads back its own payload.
        assert s1.load(r1.ref_id) == {"a": 1}
        assert s2.load(r2.ref_id) == {"b": 2}
