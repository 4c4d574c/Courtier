"""Test search plugin entry point handler logic."""

import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

from .conftest import _ensure_plugin_path

_ensure_plugin_path("search")

# Import at module level so the pluginʼs own ``tools`` module is cached
# in sys.modules before other plugin tests can shadow it.
from plugins.shared.search.entry import SearchPlugin  # noqa: E402

# Load the plugin's tools.py by path for direct _build_es_query assertions
# (same pattern as tests/agent/api/test_search_scope.py).
_TOOLS_PATH = Path(__file__).resolve().parents[2] / "plugins" / "shared" / "search" / "tools.py"
_spec = importlib.util.spec_from_file_location("search_plugin_tools_semantics", _TOOLS_PATH)
tools = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tools)


@pytest.mark.asyncio
async def test_search_plugin_registers_tools_without_system_prompt():
    plugin = SearchPlugin()
    plugin._setup_handlers()
    caps, system_prompt = plugin._collect_capabilities()
    tool_names = {c["name"] for c in caps if c["type"] == "tool"}
    assert tool_names == {"search_documents", "read_chunks"}
    # Model-facing guidance lives in core behavioral.yaml only; a plugin
    # system_prompt would drift from it (it used to duplicate citation
    # rules that were never forwarded to the host anyway).
    assert system_prompt == ""


@pytest.mark.asyncio
async def test_search_plugin_unknown_tool():
    plugin = SearchPlugin()

    output_lines: list[str] = []
    input_queue: asyncio.Queue[str] = asyncio.Queue()

    class TestWriter:
        def write(self, data: str | bytes):
            if isinstance(data, bytes):
                data = data.decode("utf-8")
            output_lines.append(data)

        def flush(self):
            pass

    plugin._reader = input_queue
    plugin._writer = TestWriter()

    req = json.dumps(
        {
            "id": 1,
            "method": "tool.execute",
            "params": {"tool": "nonexistent_tool", "args": {}},
        }
    )
    input_queue.put_nowait(req)
    input_queue.put_nowait("")  # EOF

    await plugin.run()

    # Parse responses, find the one with id=1
    responses = [json.loads(line) for line in output_lines if '"id"' in line]
    resp = next(r for r in responses if r.get("id") == 1)
    assert resp["result"]["success"] is False
    assert "Unknown tool" in resp["result"]["error"]


def _multi_match(body: dict) -> dict:
    (clause,) = [c for c in body["query"]["bool"]["must"] if "multi_match" in c]
    return clause["multi_match"]


class TestBuildEsQuerySemantics:
    def test_free_text_requires_and_operator(self):
        body = tools._build_es_query("安全生产")
        mm = _multi_match(body)
        assert mm["operator"] == "and"
        assert "minimum_should_match" not in mm

    def test_multi_word_query_uses_minimum_should_match(self):
        body = tools._build_es_query("安全生产 主体责任 落实 煤矿")
        mm = _multi_match(body)
        assert mm["minimum_should_match"] == "70%"
        assert "operator" not in mm

    def test_long_unspaced_query_uses_minimum_should_match(self):
        body = tools._build_es_query("露天煤矿边坡稳定性")
        mm = _multi_match(body)
        assert mm["minimum_should_match"] == "70%"
        assert "operator" not in mm

    def test_free_text_adds_phrase_booster(self):
        body = tools._build_es_query("安全生产主体责任")
        (booster,) = body["query"]["bool"]["should"]
        assert booster["match_phrase"]["chunk_text"] == {
            "query": "安全生产主体责任",
            "slop": 2,
            "boost": 2.0,
        }

    def test_quoted_phrase_stays_in_must(self):
        body = tools._build_es_query('关于"安全生产"的通知')
        must = body["query"]["bool"]["must"]
        (phrase,) = [c for c in must if "match_phrase" in c]
        assert phrase["match_phrase"]["chunk_text"] == {"query": "安全生产", "slop": 0}

    def test_default_fields_are_weighted(self):
        body = tools._build_es_query("安全生产")
        mm = _multi_match(body)
        assert mm["fields"] == ["chunk_text^1", "title^3"]

    def test_custom_fields_respected_without_weights(self):
        body = tools._build_es_query("安全生产", search_fields=["chunk_text", "tags"])
        mm = _multi_match(body)
        assert mm["fields"] == ["chunk_text", "tags"]

    def test_sort_is_deterministic(self):
        body = tools._build_es_query("安全生产")
        assert body["sort"] == [
            {"_score": {"order": "desc"}},
            {"publish_date": {"order": "desc", "unmapped_type": "date"}},
            {"resource_id": {"order": "asc", "unmapped_type": "long"}},
            {"chunk_no": {"order": "asc", "unmapped_type": "long"}},
        ]

    def test_highlight_uses_plain_field_names(self):
        body = tools._build_es_query("安全生产")
        assert set(body["highlight"]["fields"]) == {"chunk_text", "title"}

    def test_short_query_keeps_fuzziness(self):
        body = tools._build_es_query("通知")
        assert _multi_match(body)["fuzziness"] == "AUTO"

    def test_time_decay_off_by_default(self):
        body = tools._build_es_query("通知")
        assert "function_score" not in body["query"]

    def test_time_decay_wraps_lexical_query(self):
        body = tools._build_es_query("通知", use_time_decay=True)
        fs = body["query"]["function_score"]
        assert "bool" in fs["query"]
        assert fs["query"]["bool"]["must"]  # lexical clauses preserved
        gauss_fn, neutral_fn = fs["functions"]
        gauss = gauss_fn["gauss"]["publish_date"]
        assert gauss == {"origin": "now", "scale": "730d", "decay": 0.5}
        assert gauss_fn["filter"] == {"exists": {"field": "publish_date"}}
        # gauss has no `missing` parameter — undated chunks get neutral 1.0.
        assert neutral_fn == {
            "filter": {"bool": {"must_not": [{"exists": {"field": "publish_date"}}]}},
            "weight": 1.0,
        }


class TestHybridQuery:
    _VECTOR = [0.1, 0.2, 0.3]

    def test_build_knn_query_carries_filters(self):
        filters = [
            {"term": {"doc_type": "通知"}},
            {"bool": {"should": [], "minimum_should_match": 1}},
        ]
        body = tools._build_knn_query(self._VECTOR, filters)
        assert body == {
            "knn": {
                "field": "chunk_vector",
                "query_vector": self._VECTOR,
                "k": 50,
                "num_candidates": 200,
                "filter": filters,
            }
        }

    def test_build_knn_query_without_filters(self):
        body = tools._build_knn_query(self._VECTOR, [])
        assert "filter" not in body["knn"]

    def test_lexical_query_has_no_knn_or_rank(self):
        body = tools._build_es_query("通知")
        assert "knn" not in body
        assert "rank" not in body

    def test_rrf_fuse_ranks_shared_hit_first(self):
        lex = [
            {"_id": "a", "_score": 10.0, "_source": {"resource_id": 1, "chunk_no": 0}},
            {"_id": "b", "_score": 9.0, "_source": {"resource_id": 1, "chunk_no": 1}},
        ]
        knn = [
            {"_id": "b", "_score": 0.9, "_source": {"resource_id": 1, "chunk_no": 1}},
            {"_id": "c", "_score": 0.8, "_source": {"resource_id": 2, "chunk_no": 0}},
        ]
        fused = tools._rrf_fuse(lex, knn)
        # b appears in both lists (ranks 2 and 1), so it fuses to the top.
        assert [h["_id"] for h in fused] == ["b", "a", "c"]

    def test_rrf_fuse_lexical_hit_wins_collision(self):
        lex_hit = {
            "_id": "a",
            "_score": 9.9,
            "highlight": {"chunk_text": ["<em>x</em>"]},
            "_source": {"chunk_text": "x"},
        }
        knn_hit = {"_id": "a", "_score": 0.8, "_source": {"chunk_text": "x"}}
        fused = tools._rrf_fuse([lex_hit], [knn_hit])
        assert fused[0] is lex_hit  # lexical dict wins (keeps highlight)
        assert fused[0]["_rrf"] == 2 / (60 + 1)

    def test_synonym_expansion_adds_phrase_boosters(self):
        body = tools._build_es_query("安监局的通知")
        should = body["query"]["bool"]["should"]
        expanded = [c["match_phrase"]["chunk_text"]["query"] for c in should]
        assert "安全生产监督管理局" in expanded
        # The original free text keeps its own (stronger) phrase booster.
        assert {"query": "安监局的通知", "slop": 2, "boost": 2.0} in [
            c["match_phrase"]["chunk_text"] for c in should
        ]

    def test_synonym_expansion_does_not_alter_must(self):
        body = tools._build_es_query("安监局的通知")
        mm = _multi_match(body)
        assert mm["query"] == "安监局的通知"


class TestTimeDecay:
    """Client-side decay on fused scores (hybrid) vs server-side gauss
    (pure lexical), with an explicit flag when decay is skipped on a
    degraded path."""

    def test_decay_multiplier_values(self):
        from datetime import date

        now = date(2026, 8, 16)
        assert tools._decay_multiplier(None, now) == 1.0
        assert tools._decay_multiplier("", now) == 1.0
        assert tools._decay_multiplier("garbage", now) == 1.0
        assert tools._decay_multiplier("2026-08-16", now) == 1.0
        # one scale (730d) out → multiplier == decay (0.5)
        assert tools._decay_multiplier("2024-08-16", now) == pytest.approx(0.5)
        # half a scale out → 0.5 ** 0.25
        assert tools._decay_multiplier("2025-08-16", now) == pytest.approx(0.5**0.25)

    def test_rrf_fuse_decay_demotes_old_docs(self):
        lex = [
            {
                "_id": "old",
                "_score": 10.0,
                "_source": {"resource_id": 1, "chunk_no": 0, "publish_date": "2020-01-01"},
            },
            {
                "_id": "new",
                "_score": 9.0,
                "_source": {"resource_id": 2, "chunk_no": 0, "publish_date": "2026-08-01"},
            },
        ]
        from datetime import date

        def decay(hit):
            return tools._decay_multiplier(hit["_source"].get("publish_date"), date.today())

        fused = tools._rrf_fuse(lex, [], decay_fn=decay)
        # Equal-rank proximity: without decay "old" wins on lexical score;
        # with decay the fresh chunk must rank first.
        assert [h["_id"] for h in fused] == ["new", "old"]

    @staticmethod
    def _install_body_capture(monkeypatch, n_corpus: int, knn_hits: list[dict] | None = None):
        import es_client

        bodies: dict[str, dict] = {}

        def fake_search_chunks(query_body, skip, limit):
            body_json = json.dumps(query_body)
            if "knn" in query_body:
                bodies["knn"] = query_body
                return {"hits": {"total": {"value": 0}, "hits": knn_hits or []}, "took": 1}
            if '"range"' in body_json:
                return {"hits": {"total": {"value": 0}, "hits": []}, "took": 1}
            bodies["lexical"] = query_body
            corpus = [_corpus_hit(i) for i in range(n_corpus)]
            return {
                "hits": {"total": {"value": n_corpus}, "hits": corpus[skip : skip + limit]},
                "took": 1,
            }

        monkeypatch.setattr(es_client, "search_chunks", fake_search_chunks)
        return bodies

    @pytest.mark.asyncio
    async def test_hybrid_decay_is_client_side_not_in_body(self, monkeypatch):
        tools._cache_clear()
        import embeddings

        bodies = self._install_body_capture(monkeypatch, n_corpus=5)

        async def fake_embed(query):
            return [0.1, 0.2, 0.3]

        monkeypatch.setattr(embeddings, "embedding_config", lambda: object())
        monkeypatch.setattr(embeddings, "embed_query", fake_embed)

        result = await tools.SearchDocumentsTool().execute(
            query="通知", use_time_decay=True, limit=5
        )
        assert result.success
        assert result.data["mode"] == "hybrid"
        # No server-side gauss on the lexical arm (it would double-decay).
        assert "function_score" not in json.dumps(bodies["lexical"])
        assert result.data["time_decay_applied"] is True

    @pytest.mark.asyncio
    async def test_pure_lexical_decay_uses_server_side_gauss(self, monkeypatch):
        tools._cache_clear()
        bodies = self._install_body_capture(monkeypatch, n_corpus=5)
        monkeypatch.delenv("LLM_EMBEDDING_NAME", raising=False)

        result = await tools.SearchDocumentsTool().execute(
            query="通知", use_time_decay=True, limit=5
        )
        assert result.success
        assert "function_score" in json.dumps(bodies["lexical"])
        assert result.data["time_decay_applied"] is True

    @pytest.mark.asyncio
    async def test_degraded_hybrid_loses_decay_and_flags_it(self, monkeypatch):
        tools._cache_clear()
        import embeddings

        bodies = self._install_body_capture(monkeypatch, n_corpus=5)

        async def failing_embed(query):
            raise RuntimeError("boom")

        monkeypatch.setattr(embeddings, "embedding_config", lambda: object())
        monkeypatch.setattr(embeddings, "embed_query", failing_embed)

        result = await tools.SearchDocumentsTool().execute(
            query="通知", use_time_decay=True, limit=5
        )
        assert result.success
        assert result.data["mode"] == "lexical"
        # Hybrid planned → body built without gauss; embed failed → decay
        # skipped, explicitly flagged.
        assert "function_score" not in json.dumps(bodies["lexical"])
        assert result.data["time_decay_applied"] is False


def _neighbor_hit(resource_id: int, chunk_no: int) -> dict:
    return {"resource_id": resource_id, "chunk_no": chunk_no, "chunk_text": "x", "title": "t"}


def _corpus_hit(i: int) -> dict:
    return {
        "_id": f"lex-{i}",
        "_score": 100.0 - i,
        "_source": {
            "resource_id": 1,
            "chunk_no": i,
            "title": f"t{i}",
            "chunk_text": f"内容{i}",
            "chunk_text_preview": f"内容{i}",
        },
    }


class TestFetchWindow:
    """Hybrid/rerank pages slice an in-process fused list, so both arms
    must fetch the full window (skip+limit) — the old behavior fetched only
    the first `limit` lexical hits, silently keeping deep lexical hits out
    of fusion on page 2+."""

    @staticmethod
    def _install_es(monkeypatch, n_corpus: int, knn_hits: list[dict] | None = None):
        """Capture (mode, skip, limit) per ES call; neighbor queries → empty."""
        import es_client

        calls: list[tuple[str, int, int]] = []

        def fake_search_chunks(query_body, skip, limit):
            body_json = json.dumps(query_body)
            if "knn" in query_body:
                mode = "knn"
            elif '"range"' in body_json:
                mode = "neighbor"
            else:
                mode = "lexical"
            calls.append((mode, skip, limit))
            if mode == "knn":
                return {"hits": {"total": {"value": 0}, "hits": knn_hits or []}, "took": 1}
            if mode == "neighbor":
                return {"hits": {"total": {"value": 0}, "hits": []}, "took": 1}
            corpus = [_corpus_hit(i) for i in range(n_corpus)]
            return {
                "hits": {"total": {"value": n_corpus}, "hits": corpus[skip : skip + limit]},
                "took": 1,
            }

        monkeypatch.setattr(es_client, "search_chunks", fake_search_chunks)
        return calls

    @staticmethod
    def _enable_hybrid(monkeypatch):
        import embeddings

        async def fake_embed(query):
            return [0.1, 0.2, 0.3]

        monkeypatch.setattr(embeddings, "embedding_config", lambda: object())
        monkeypatch.setattr(embeddings, "embed_query", fake_embed)

    @pytest.mark.asyncio
    async def test_hybrid_lexical_arm_fetches_full_window_per_page(self, monkeypatch):
        tools._cache_clear()
        calls = self._install_es(monkeypatch, n_corpus=30)
        self._enable_hybrid(monkeypatch)

        tool = tools.SearchDocumentsTool()
        result = await tool.execute(query="通知", skip=10, limit=10)
        assert result.success
        # Page 2: window = min(max(10+10, 50), 200) = 50 — the lexical arm
        # must fetch all 50, not just the 10-hit first page.
        lexical_calls = [c for c in calls if c[0] == "lexical"]
        assert lexical_calls == [("lexical", 0, 50)]
        knn_calls = [c for c in calls if c[0] == "knn"]
        assert knn_calls == [("knn", 0, 50)]
        # kNN arm empty → fused list is lexical order; page 2 = corpus[10:20].
        assert [h["chunk_no"] for h in result.data["hits"]] == list(range(10, 20))
        assert result.data["total_mode"] == "window"
        assert result.data["total"] == 30
        assert result.data["mode"] == "hybrid"

    @pytest.mark.asyncio
    async def test_hybrid_pages_are_disjoint_and_complete(self, monkeypatch):
        tools._cache_clear()
        self._install_es(monkeypatch, n_corpus=30)
        self._enable_hybrid(monkeypatch)

        tool = tools.SearchDocumentsTool()
        pages = []
        for skip in (0, 10, 20):
            result = await tool.execute(query="通知", skip=skip, limit=10)
            pages.extend(h["chunk_no"] for h in result.data["hits"])
        assert pages == list(range(30))

    @pytest.mark.asyncio
    async def test_deep_skip_beyond_window_returns_empty_page(self, monkeypatch):
        tools._cache_clear()
        calls = self._install_es(monkeypatch, n_corpus=300)
        self._enable_hybrid(monkeypatch)

        tool = tools.SearchDocumentsTool()
        result = await tool.execute(query="通知", skip=250, limit=10)
        assert result.success
        assert result.data["hits"] == []
        # window capped: lexical fetched 200, not 260.
        assert ("lexical", 0, 200) in [c for c in calls if c[0] == "lexical"]

    @pytest.mark.asyncio
    async def test_pure_lexical_keeps_native_paging_and_exact_total(self, monkeypatch):
        tools._cache_clear()
        calls = self._install_es(monkeypatch, n_corpus=30)
        monkeypatch.delenv("LLM_EMBEDDING_NAME", raising=False)

        tool = tools.SearchDocumentsTool()
        result = await tool.execute(query="通知", skip=10, limit=10)
        assert result.success
        assert [c for c in calls if c[0] == "lexical"] == [("lexical", 10, 10)]
        assert not [c for c in calls if c[0] == "knn"]
        assert result.data["mode"] == "lexical"
        assert result.data["total_mode"] == "exact"
        assert result.data["total"] == 30
        assert [h["chunk_no"] for h in result.data["hits"]] == list(range(10, 20))

    @pytest.mark.asyncio
    async def test_rerank_fetch_capped_at_100(self, monkeypatch):
        tools._cache_clear()
        import rerank

        calls = self._install_es(monkeypatch, n_corpus=300)
        monkeypatch.delenv("LLM_EMBEDDING_NAME", raising=False)

        async def fake_rerank(query, hits):
            return hits, False

        monkeypatch.setattr(rerank, "rerank_hits", fake_rerank)

        tool = tools.SearchDocumentsTool()
        result = await tool.execute(query="通知", rerank=True, skip=85, limit=10)
        assert result.success
        # window = min(85+10, 200) = 95; fetch from 0, no extra cap applied.
        assert [c for c in calls if c[0] == "lexical"] == [("lexical", 0, 95)]
        assert [h["chunk_no"] for h in result.data["hits"]] == list(range(85, 95))


class TestParallelRetrieval:
    """The lexical fetch and the query embedding run concurrently; the kNN
    arm follows once the vector resolves."""

    @pytest.mark.asyncio
    async def test_embed_runs_concurrently_with_lexical_fetch(self, monkeypatch):
        tools._cache_clear()
        import embeddings
        import es_client

        loop = asyncio.get_running_loop()
        lexical_done = asyncio.Event()

        def fake_search_chunks(query_body, skip, limit):
            body_json = json.dumps(query_body)
            if "knn" in query_body or '"range"' in body_json:
                return {"hits": {"total": {"value": 0}, "hits": []}, "took": 1}
            loop.call_soon_threadsafe(lexical_done.set)
            return {"hits": {"total": {"value": 1}, "hits": [_corpus_hit(0)]}, "took": 1}

        monkeypatch.setattr(es_client, "search_chunks", fake_search_chunks)

        async def fake_embed(query):
            # Only resolvable while the lexical fetch is executing: proves
            # the two overlap.  A sequential embed-then-lexical order would
            # deadlock here and fail the 3s timeout.
            await asyncio.wait_for(lexical_done.wait(), timeout=3)
            return [0.1, 0.2, 0.3]

        monkeypatch.setattr(embeddings, "embedding_config", lambda: object())
        monkeypatch.setattr(embeddings, "embed_query", fake_embed)

        result = await tools.SearchDocumentsTool().execute(query="通知", limit=5)
        assert result.success
        assert result.data["mode"] == "hybrid"

    @pytest.mark.asyncio
    async def test_embed_failure_degrades_to_window_sliced_lexical(self, monkeypatch):
        tools._cache_clear()
        import embeddings

        calls = TestFetchWindow._install_es(monkeypatch, n_corpus=30)

        async def failing_embed(query):
            raise RuntimeError("boom")

        monkeypatch.setattr(embeddings, "embedding_config", lambda: object())
        monkeypatch.setattr(embeddings, "embed_query", failing_embed)

        result = await tools.SearchDocumentsTool().execute(query="通知", skip=10, limit=10)
        assert result.success
        assert result.data["mode"] == "lexical"
        # Embedding was planned → window fetch (not native skip=10 page);
        # the degraded result must still be sliced in-process to the page.
        assert [c for c in calls if c[0] == "lexical"] == [("lexical", 0, 50)]
        assert [h["chunk_no"] for h in result.data["hits"]] == list(range(10, 20))


class TestNeighborExpansion:
    def test_build_neighbor_query_windows(self):
        body = tools._build_neighbor_query(
            [_neighbor_hit(1, 5), _neighbor_hit(2, 0)], tools._QUERY_UNSET
        )
        clause_one, clause_two = body["query"]["bool"]["should"]
        assert clause_one["bool"]["must"] == [
            {"term": {"resource_id": 1}},
            {"range": {"chunk_no": {"gte": 3, "lte": 7}}},
        ]
        assert clause_two["bool"]["must"] == [
            {"term": {"resource_id": 2}},
            {"range": {"chunk_no": {"gte": -2, "lte": 2}}},
        ]
        assert "filter" not in body["query"]["bool"]

    def test_build_neighbor_query_carries_owner_scope(self):
        body = tools._build_neighbor_query([_neighbor_hit(1, 5)], owner_scope=7)
        (filter_clause,) = body["query"]["bool"]["filter"]
        assert {"term": {"owner_id": 7}} in filter_clause["bool"]["should"]

    def test_build_neighbor_query_empty_without_coordinates(self):
        assert tools._build_neighbor_query([{"chunk_text": "no coords"}], None) == {}

    def test_clean_neighbor_hits_extracts_previews(self):
        raw = {
            "hits": {
                "hits": [
                    {
                        "_source": {
                            "resource_id": 1,
                            "chunk_no": 4,
                            "title": "标题",
                            "chunk_text": "内容" * 200,
                        }
                    }
                ]
            }
        }
        entries = tools._clean_neighbor_hits(raw)
        assert len(entries) == 1
        rid, cno, entry = entries[0]
        assert (rid, cno) == (1, 4)
        assert entry["title"] == "标题"
        assert len(entry["chunk_text_preview"]) == tools._NEIGHBOR_PREVIEW_CHARS

    def test_attach_neighbors_respects_window(self):
        # Two hits far apart in one resource: entries from both windows are
        # fetched in one query, but each hit only keeps its own +/-2 window.
        hits = [{"resource_id": 1, "chunk_no": 0}, {"resource_id": 1, "chunk_no": 10}]
        entries = [
            (1, 0, {"chunk_no": 0}),
            (1, 1, {"chunk_no": 1}),
            (1, 2, {"chunk_no": 2}),
            (1, 9, {"chunk_no": 9}),
            (1, 10, {"chunk_no": 10}),
            (1, 11, {"chunk_no": 11}),
        ]
        tools._attach_neighbors(hits, entries)
        assert [n["chunk_no"] for n in hits[0]["neighbors"]] == [1, 2]
        assert [n["chunk_no"] for n in hits[1]["neighbors"]] == [9, 11]


class TestEmbeddingClient:
    """The plugin embedding client reads env config and talks to the
    OpenAI-compatible /embeddings endpoint via httpx."""

    import httpx

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
            return TestEmbeddingClient._FakeResponse(
                {"data": [{"index": i, "embedding": [float(i)] * 3} for i in range(len(texts))]}
            )

    @pytest.mark.asyncio
    async def test_embed_not_configured(self, monkeypatch):
        import embeddings

        monkeypatch.delenv("LLM_EMBEDDING_NAME", raising=False)
        monkeypatch.delenv("LLM_IP", raising=False)
        assert embeddings.embedding_config() is None
        with pytest.raises(embeddings.EmbeddingUnavailable):
            await embeddings.embed_texts(["查询"])

    @pytest.mark.asyncio
    async def test_embed_happy_path_and_batching(self, monkeypatch):
        import embeddings

        monkeypatch.setenv("LLM_EMBEDDING_NAME", "text-embedding-v3")
        monkeypatch.setenv("LLM_IP", "https://example.com/v1")
        monkeypatch.setenv("LLM_API_KEY", "k")
        monkeypatch.setenv("LLM_EMBEDDING_BATCH_SIZE", "2")
        self._FakeAsyncClient.calls.clear()
        monkeypatch.setattr(self.httpx, "AsyncClient", self._FakeAsyncClient)

        vectors = await embeddings.embed_texts(["甲", "乙", "丙"])
        # Fake embeddings are indexed within each batch: [0.0], [1.0] in
        # batch one, [0.0] in batch two.
        assert vectors == [[0.0] * 3, [1.0] * 3, [0.0] * 3]
        # batched 2+1
        assert len(self._FakeAsyncClient.calls) == 2
        for _url, payload in self._FakeAsyncClient.calls:
            assert payload["model"] == "text-embedding-v3"

    @pytest.mark.asyncio
    async def test_embed_failure_raises_after_retry(self, monkeypatch):
        import embeddings

        monkeypatch.setenv("LLM_EMBEDDING_NAME", "text-embedding-v3")
        monkeypatch.setenv("LLM_IP", "https://example.com/v1")

        class FailingClient(self._FakeAsyncClient):
            async def post(self, url, json=None, headers=None):
                raise RuntimeError("boom")

        monkeypatch.setattr(self.httpx, "AsyncClient", FailingClient)
        with pytest.raises(embeddings.EmbeddingUnavailable):
            await embeddings.embed_texts(["甲"])


class TestRerankEvidenceWindow:
    """Adaptive per-candidate length + segment localization in the rerank
    prompt: relevant clauses beyond the first 200 chars used to be cut out
    of the evidence the rerank LLM sees."""

    def test_candidate_chars_budget(self):
        import rerank

        assert rerank._candidate_chars(1) == rerank._CANDIDATE_MAX_CHARS
        assert rerank._candidate_chars(10) == 800  # 2400 clamped to max
        assert rerank._candidate_chars(50) == 24_000 // 50  # 480
        assert rerank._candidate_chars(500) == rerank._CANDIDATE_MIN_CHARS

    def test_query_grams_mix_phrases_and_bigrams(self):
        import rerank

        grams = rerank._query_grams('关于"安全生产"的通知')
        assert "安全生产" in grams  # quoted phrase kept whole
        assert "通知" in grams  # bigram of free text
        assert "关于" in grams

    def test_best_window_centers_on_relevant_sentence(self):
        import rerank

        filler = "各单位应当加强日常管理，做好统筹协调工作。" * 10  # ~250 chars
        clause = "落实安全生产责任制，实行党政同责、一岗双责。"
        tail = "其他事项按照有关规定执行。" * 20
        text = filler + clause + tail
        grams = rerank._query_grams("安全生产责任制 落实")

        window = rerank._best_window(grams, text, chars=200)
        assert "安全生产责任制" in window
        assert window.startswith("…")  # relevant part is beyond the head
        assert len(window) <= 202  # 200 + up to 2 ellipsis markers

    def test_best_window_falls_back_to_head_without_overlap(self):
        import rerank

        text = "完全无关的内容。" * 100
        window = rerank._best_window({"zz"}, text, chars=100)
        assert window == text[:100]

    def test_best_window_short_text_untouched(self):
        import rerank

        text = "短文本。"
        assert rerank._best_window({"短文"}, text, chars=100) == text

    def test_prompt_carries_localized_evidence(self):
        import rerank

        filler = "各单位应当加强日常管理，做好统筹协调工作。" * 30
        clause = "特种作业人员必须持证上岗，严禁无证操作。"
        hit = {
            "title": "管理办法",
            "chunk_text": filler + clause + filler,
            "chunk_no": 1,
        }
        prompt = rerank._prompt("特种作业 持证上岗", [hit])
        assert "持证上岗" in prompt
        assert "候选为原文节选" in prompt
        # The clause sits ~750 chars in; only a localized window reaches it.
        assert "…" in prompt


class TestReadChunks:
    """Coordinate-based full chunk read-back with owner-scope visibility."""

    @staticmethod
    def _install(monkeypatch, corpus: dict[tuple[int, int], str], bodies: list | None = None):
        import es_client

        def fake_search_chunks(query_body, skip, limit):
            if bodies is not None:
                bodies.append(query_body)
            should = query_body["query"]["bool"]["should"]
            matched: set[tuple[int, int]] = set()
            for clause in should:
                must = clause["bool"]["must"]
                rid = must[0]["term"]["resource_id"]
                cc = must[1]
                if "term" in cc:
                    coord = (rid, cc["term"]["chunk_no"])
                    if coord in corpus:
                        matched.add(coord)
                else:
                    rng = cc["range"]["chunk_no"]
                    for cno in range(rng["gte"], rng["lte"] + 1):
                        if (rid, cno) in corpus:
                            matched.add((rid, cno))
            hits = [
                {
                    "_id": f"{rid}-{cno}",
                    "_source": {
                        "resource_id": rid,
                        "chunk_no": cno,
                        "title": f"t{rid}",
                        "doc_type": "通知",
                        "publish_date": "2026-01-01",
                        "chunk_text": corpus[(rid, cno)],
                    },
                }
                for rid, cno in sorted(matched)
            ]
            return {"hits": {"total": {"value": len(hits)}, "hits": hits}, "took": 1}

        monkeypatch.setattr(es_client, "search_chunks", fake_search_chunks)

    @pytest.mark.asyncio
    async def test_reads_exact_coordinates_in_request_order(self, monkeypatch):
        corpus = {(1, 5): "甲" * 100, (2, 3): "乙" * 100}
        self._install(monkeypatch, corpus)

        result = await tools.ReadChunksTool().execute(
            chunks=[{"resource_id": 2, "chunk_no": 3}, {"resource_id": 1, "chunk_no": 5}]
        )
        assert result.success
        assert [(c["resource_id"], c["chunk_no"]) for c in result.data["chunks"]] == [(2, 3), (1, 5)]
        assert result.data["chunks"][0]["chunk_text"] == "乙" * 100
        assert "missing" not in result.data

    @pytest.mark.asyncio
    async def test_absent_coordinates_reported_as_missing(self, monkeypatch):
        self._install(monkeypatch, {(1, 5): "甲"})
        result = await tools.ReadChunksTool().execute(
            chunks=[{"resource_id": 1, "chunk_no": 5}, {"resource_id": 9, "chunk_no": 9}]
        )
        assert result.success
        assert result.data["missing"] == [{"resource_id": 9, "chunk_no": 9}]

    @pytest.mark.asyncio
    async def test_over_limit_rejected(self):
        result = await tools.ReadChunksTool().execute(
            chunks=[{"resource_id": 1, "chunk_no": i} for i in range(11)]
        )
        assert not result.success
        assert "最多" in result.error

    @pytest.mark.asyncio
    async def test_duplicate_coordinates_deduped(self, monkeypatch):
        self._install(monkeypatch, {(1, 5): "甲"})
        result = await tools.ReadChunksTool().execute(
            chunks=[
                {"resource_id": 1, "chunk_no": 5},
                {"resource_id": 1, "chunk_no": 5},
            ]
        )
        assert result.success
        assert len(result.data["chunks"]) == 1

    @pytest.mark.asyncio
    async def test_owner_scope_filter_applied_to_query(self, monkeypatch):
        bodies: list = []
        self._install(monkeypatch, {(1, 5): "甲"}, bodies=bodies)

        await tools.ReadChunksTool().execute(
            chunks=[{"resource_id": 1, "chunk_no": 5}], _owner_scope=7
        )
        body = bodies[0]
        scope = body["query"]["bool"]["filter"][0]
        assert {"term": {"owner_id": 7}} in scope["bool"]["should"]

    @pytest.mark.asyncio
    async def test_with_neighbors_returns_adjacent_chunks(self, monkeypatch):
        corpus = {(1, 4): "四", (1, 5): "五", (1, 6): "六"}
        self._install(monkeypatch, corpus)

        result = await tools.ReadChunksTool().execute(
            chunks=[{"resource_id": 1, "chunk_no": 5}], with_neighbors=True
        )
        assert result.success
        # Requested coordinate first; neighbor extras follow in chunk order.
        assert [c["chunk_no"] for c in result.data["chunks"]] == [5, 4, 6]

    @pytest.mark.asyncio
    async def test_long_chunk_truncated_with_flag(self, monkeypatch):
        self._install(monkeypatch, {(1, 5): "长" * 5000})
        result = await tools.ReadChunksTool().execute(chunks=[{"resource_id": 1, "chunk_no": 5}])
        assert result.success
        assert len(result.data["chunks"][0]["chunk_text"]) == tools._READ_CHUNK_MAX_CHARS + 1
        assert result.data["truncated"] is True

    @pytest.mark.asyncio
    async def test_registered_with_skip_persist(self):
        plugin = SearchPlugin()
        plugin._setup_handlers()
        caps, _ = plugin._collect_capabilities()
        by_name = {c["name"]: c for c in caps if c.get("type") == "tool"}
        assert "read_chunks" in by_name
        assert by_name["read_chunks"]["skip_persist"] is True
        assert by_name["search_documents"].get("skip_persist") is None


def _rerank_hit(i: int) -> dict:
    return {"title": f"标题{i}", "chunk_text": f"内容{i}", "chunk_no": i}


class TestRerank:
    import httpx

    class _FakeRerankResponse:
        def __init__(self, content):
            self._content = content

        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": self._content}}]}

    class _FakeAsyncClient:
        response_content = "[3, 1, 2]"

        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json=None, headers=None):
            return TestRerank._FakeRerankResponse(type(self).response_content)

    def test_parse_order_plain(self):
        import rerank

        assert rerank._parse_order("[3, 1, 2]", 3) == [3, 1, 2]

    def test_parse_order_tolerates_prose(self):
        import rerank

        assert rerank._parse_order("根据相关性排序如下：\n[2, 1]\n完毕", 2) == [2, 1]

    def test_parse_order_filters_invalid(self):
        import rerank

        assert rerank._parse_order("[1, 9, 1, 2]", 3) == [1, 2]  # out-of-range/dup dropped
        assert rerank._parse_order("无法排序", 3) is None

    @pytest.mark.asyncio
    async def test_rerank_hits_reorders(self, monkeypatch):
        import rerank

        monkeypatch.setenv("LLM_IP", "https://example.com/v1")
        monkeypatch.setenv("LLM_NAME", "qwen")
        self._FakeAsyncClient.response_content = "[3, 1, 2]"
        monkeypatch.setattr(self.httpx, "AsyncClient", self._FakeAsyncClient)

        hits = [_rerank_hit(i) for i in (1, 2, 3)]
        ordered, partial = await rerank.rerank_hits("查询", hits)
        assert [h["chunk_no"] for h in ordered] == [3, 1, 2]
        assert partial is False

    @pytest.mark.asyncio
    async def test_rerank_hits_appends_unranked_remainder(self, monkeypatch):
        import rerank

        monkeypatch.setenv("LLM_IP", "https://example.com/v1")
        monkeypatch.setenv("LLM_NAME", "qwen")
        # Model returned only 6 of 10 candidates (>= 50% coverage): the
        # other 4 must be appended in original order, not dropped.
        self._FakeAsyncClient.response_content = "[3, 1, 2, 4, 5, 6]"
        monkeypatch.setattr(self.httpx, "AsyncClient", self._FakeAsyncClient)

        hits = [_rerank_hit(i) for i in range(1, 11)]
        ordered, partial = await rerank.rerank_hits("查询", hits)
        assert [h["chunk_no"] for h in ordered] == [3, 1, 2, 4, 5, 6, 7, 8, 9, 10]
        assert partial is True

    @pytest.mark.asyncio
    async def test_rerank_hits_low_coverage_keeps_original_order(self, monkeypatch):
        import rerank

        monkeypatch.setenv("LLM_IP", "https://example.com/v1")
        monkeypatch.setenv("LLM_NAME", "qwen")
        # 1 of 10 (< 50%): the ordering is untrustworthy, keep input order.
        self._FakeAsyncClient.response_content = "[2]"
        monkeypatch.setattr(self.httpx, "AsyncClient", self._FakeAsyncClient)

        hits = [_rerank_hit(i) for i in range(1, 11)]
        ordered, partial = await rerank.rerank_hits("查询", hits)
        assert [h["chunk_no"] for h in ordered] == list(range(1, 11))
        assert partial is True

    @pytest.mark.asyncio
    async def test_rerank_failure_raises(self, monkeypatch):
        import rerank

        monkeypatch.setenv("LLM_IP", "https://example.com/v1")
        monkeypatch.setenv("LLM_NAME", "qwen")

        class FailingClient(self._FakeAsyncClient):
            async def post(self, url, json=None, headers=None):
                raise RuntimeError("boom")

        monkeypatch.setattr(self.httpx, "AsyncClient", FailingClient)
        hits = [_rerank_hit(i) for i in (1, 2, 3)]
        with pytest.raises(RuntimeError):
            await rerank.rerank_hits("查询", hits)

    @pytest.mark.asyncio
    async def test_execute_rerank_reorders_and_slices(self, monkeypatch):
        import es_client
        import rerank

        monkeypatch.delenv("LLM_EMBEDDING_NAME", raising=False)

        def fake_search_chunks(query_body, skip, limit):
            hits = [
                {
                    "_id": f"{i}",
                    "_score": 9.0 - i,
                    "_source": {
                        "resource_id": 1,
                        "chunk_no": i,
                        "title": f"t{i}",
                        "chunk_text": f"c{i}",
                    },
                }
                for i in range(3)
            ]
            return {"hits": {"total": {"value": 3}, "hits": hits}, "took": 1}

        monkeypatch.setattr(es_client, "search_chunks", fake_search_chunks)
        reverse = [_rerank_hit(3), _rerank_hit(2), _rerank_hit(1)]

        async def fake_rerank(query, hits):
            return reverse, False

        monkeypatch.setattr(rerank, "rerank_hits", fake_rerank)

        tool = tools.SearchDocumentsTool()
        result = await tool.execute(query="查询", rerank=True, limit=2)
        assert result.success
        assert result.data["reranked"] is True
        assert result.data["rerank_partial"] is False
        assert [h["chunk_no"] for h in result.data["hits"]] == [3, 2]


class TestResultCache:
    def test_cache_roundtrip_and_expiry(self, monkeypatch):
        tools._cache_clear()
        key = tools._cache_key(
            False,
            query="x",
            _owner_scope=1,
            document_id=None,
            doc_type=None,
            tags=None,
            search_fields=None,
            skip=0,
            limit=10,
            include_annotations=False,
        )
        tools._cache_put(key, {"hits": [1]})
        assert tools._cache_get(key) == {"hits": [1]}
        monkeypatch.setattr(tools, "_CACHE_TTL_SECONDS", -1)
        tools._cache_put(key, {"hits": [2]})
        assert tools._cache_get(key) is None

    def test_cache_key_distinguishes_scope_and_rerank(self):
        k1 = tools._cache_key(False, query="x", _owner_scope=1)
        k2 = tools._cache_key(False, query="x", _owner_scope=2)
        k3 = tools._cache_key(True, query="x", _owner_scope=1)
        assert len({k1, k2, k3}) == 3

    @pytest.mark.asyncio
    async def test_execute_second_call_served_from_cache(self, monkeypatch):
        import es_client

        tools._cache_clear()
        monkeypatch.delenv("LLM_EMBEDDING_NAME", raising=False)
        calls: list[int] = []

        def fake_search_chunks(query_body, skip, limit):
            calls.append(1)
            hits = [
                {
                    "_id": f"{i}",
                    "_score": 9.0 - i,
                    "_source": {"title": f"t{i}", "chunk_text": f"c{i}"},
                }
                for i in range(3)
            ]
            return {"hits": {"total": {"value": 3}, "hits": hits}, "took": 1}

        monkeypatch.setattr(es_client, "search_chunks", fake_search_chunks)
        tool = tools.SearchDocumentsTool()

        first = await tool.execute(query="查询", limit=3)
        assert first.success and first.data.get("cached") is None
        second = await tool.execute(query="查询", limit=3)
        assert second.success and second.data.get("cached") is True
        # No neighbor expansion (hits carry no resource_id/chunk_no), so the
        # only ES round-trip is the first call's lexical search.
        assert len(calls) == 1

class TestLimitCap:
    def test_limit_over_50_rejected(self):
        import asyncio

        result = asyncio.run(
            tools.SearchDocumentsTool().execute(query="通知", limit=51)
        )
        assert not result.success
        assert "1 到 50" in result.error



class TestRankField:
    """Every returned hit carries a page-relative 1-based rank, assigned
    after slicing/reordering so it always matches the returned page."""

    @pytest.mark.asyncio
    async def test_hybrid_page_ranks_restart_at_one(self, monkeypatch):
        tools._cache_clear()
        TestFetchWindow._install_es(monkeypatch, n_corpus=30)
        TestFetchWindow._enable_hybrid(monkeypatch)

        result = await tools.SearchDocumentsTool().execute(query="通知", skip=10, limit=10)
        assert [h["rank"] for h in result.data["hits"]] == list(range(1, 11))

    @pytest.mark.asyncio
    async def test_rerank_ranks_follow_reordered_page(self, monkeypatch):
        tools._cache_clear()
        import rerank

        TestFetchWindow._install_es(monkeypatch, n_corpus=10)
        monkeypatch.delenv("LLM_EMBEDDING_NAME", raising=False)

        async def fake_rerank(query, hits):
            return list(reversed(hits)), False

        monkeypatch.setattr(rerank, "rerank_hits", fake_rerank)

        result = await tools.SearchDocumentsTool().execute(query="通知", rerank=True, limit=5)
        chunk_nos = [h["chunk_no"] for h in result.data["hits"]]
        assert chunk_nos == [9, 8, 7, 6, 5]  # reversed order
        assert [h["rank"] for h in result.data["hits"]] == [1, 2, 3, 4, 5]

    @pytest.mark.asyncio
    async def test_cache_hit_still_carries_ranks(self, monkeypatch):
        tools._cache_clear()
        TestFetchWindow._install_es(monkeypatch, n_corpus=30)
        TestFetchWindow._enable_hybrid(monkeypatch)

        tool = tools.SearchDocumentsTool()
        first = await tool.execute(query="缓存查询", limit=5)
        second = await tool.execute(query="缓存查询", limit=5)
        assert second.data.get("cached") is True
        assert [h["rank"] for h in second.data["hits"]] == list(range(1, 6))
