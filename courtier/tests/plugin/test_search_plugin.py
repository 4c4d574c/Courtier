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
        bodies = self._install_body_capture(monkeypatch, n_corpus=5)

        result = await tools.SearchDocumentsTool().execute(
            query="通知",
            use_time_decay=True,
            limit=5,
            query_embedding=[0.1, 0.2, 0.3],
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

        result = await tools.SearchDocumentsTool().execute(
            query="通知", use_time_decay=True, limit=5
        )
        assert result.success
        assert "function_score" in json.dumps(bodies["lexical"])
        assert result.data["time_decay_applied"] is True


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
    def _enable_hybrid(_monkeypatch):
        """Hybrid mode is host-driven now: return injected-vector kwargs."""
        return {"query_embedding": [0.1, 0.2, 0.3]}

    @pytest.mark.asyncio
    async def test_hybrid_lexical_arm_fetches_full_window_per_page(self, monkeypatch):
        tools._cache_clear()
        calls = self._install_es(monkeypatch, n_corpus=30)
        _hybrid = self._enable_hybrid(monkeypatch)

        tool = tools.SearchDocumentsTool()
        result = await tool.execute(query="通知", skip=10, limit=10, **_hybrid)
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
        _hybrid = self._enable_hybrid(monkeypatch)

        tool = tools.SearchDocumentsTool()
        pages = []
        for skip in (0, 10, 20):
            result = await tool.execute(query="通知", skip=skip, limit=10, **_hybrid)
            pages.extend(h["chunk_no"] for h in result.data["hits"])
        assert pages == list(range(30))

    @pytest.mark.asyncio
    async def test_deep_skip_beyond_window_returns_empty_page(self, monkeypatch):
        tools._cache_clear()
        calls = self._install_es(monkeypatch, n_corpus=300)
        _hybrid = self._enable_hybrid(monkeypatch)

        tool = tools.SearchDocumentsTool()
        result = await tool.execute(query="通知", skip=250, limit=10, **_hybrid)
        assert result.success
        assert result.data["hits"] == []
        # window capped: lexical fetched 200, not 260.
        assert ("lexical", 0, 200) in [c for c in calls if c[0] == "lexical"]

    @pytest.mark.asyncio
    async def test_pure_lexical_keeps_native_paging_and_exact_total(self, monkeypatch):
        tools._cache_clear()
        calls = self._install_es(monkeypatch, n_corpus=30)

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
    async def test_rerank_fetch_window_coarse(self, monkeypatch):
        tools._cache_clear()
        calls = self._install_es(monkeypatch, n_corpus=300)

        tool = tools.SearchDocumentsTool()
        result = await tool.execute(query="通知", rerank=True, skip=85, limit=10)
        assert result.success
        # window = min(85+10, 200) = 95; fetch from 0 — the coarse result
        # feeds the host reranker (pagination happens in the finalize pass).
        assert [c for c in calls if c[0] == "lexical"] == [("lexical", 0, 95)]
        assert [h["chunk_no"] for h in result.data["hits"]] == list(range(95))
        assert "reranked" not in result.data
        assert not [c for c in calls if c[0] == "neighbor"]


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
        coords = [(c["resource_id"], c["chunk_no"]) for c in result.data["chunks"]]
        assert coords == [(2, 3), (1, 5)]
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


class TestRerankCoarseAndFinalize:
    """rerank=true returns the coarse candidate window for the host-side
    reranker; the finalize pass paginates + neighbor-expands the reordered
    payload."""

    @pytest.mark.asyncio
    async def test_rerank_returns_coarse_and_caches(self, monkeypatch):
        tools._cache_clear()
        TestFetchWindow._install_es(monkeypatch, n_corpus=30)

        tool = tools.SearchDocumentsTool()
        first = await tool.execute(query="通知", rerank=True, limit=10)
        assert first.success
        # Coarse: full window unpaged, no rerank flags, no neighbors.
        assert [h["chunk_no"] for h in first.data["hits"]] == list(range(30))
        assert "reranked" not in first.data
        second = await tool.execute(query="通知", rerank=True, limit=10)
        assert second.success
        assert second.data.get("cached") is True

    @pytest.mark.asyncio
    async def test_finalize_slices_flags_and_ranks(self, monkeypatch):
        tools._cache_clear()
        TestFetchWindow._install_es(monkeypatch, n_corpus=10)

        hits = [
            {"resource_id": 1, "chunk_no": i, "chunk_text": f"c{i}", "title": f"t{i}"}
            for i in range(10)
        ]
        payload = {
            "total": 10,
            "hits": list(reversed(hits)),
            "reranked": True,
            "rerank_partial": False,
        }
        result = await tools.SearchDocumentsTool().execute(
            query="通知", finalize=payload, skip=2, limit=3
        )
        assert result.success
        assert [h["chunk_no"] for h in result.data["hits"]] == [7, 6, 5]
        assert [h["rank"] for h in result.data["hits"]] == [1, 2, 3]
        assert result.data["reranked"] is True
        assert result.data["rerank_partial"] is False

    @pytest.mark.asyncio
    async def test_finalize_failure_flags_pass_through(self):
        payload = {"total": 0, "hits": [], "reranked": False, "rerank_partial": True}
        result = await tools.SearchDocumentsTool().execute(
            query="通知", finalize=payload, skip=0, limit=10
        )
        assert result.success
        assert result.data["rerank_partial"] is True


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
        _hybrid = TestFetchWindow._enable_hybrid(monkeypatch)

        result = await tools.SearchDocumentsTool().execute(
            query="通知", skip=10, limit=10, **_hybrid
        )
        assert [h["rank"] for h in result.data["hits"]] == list(range(1, 11))

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_cache_hit_still_carries_ranks(self, monkeypatch):
        tools._cache_clear()
        TestFetchWindow._install_es(monkeypatch, n_corpus=30)
        _hybrid = TestFetchWindow._enable_hybrid(monkeypatch)

        tool = tools.SearchDocumentsTool()
        await tool.execute(query="缓存查询", limit=5, **_hybrid)  # warm the cache
        second = await tool.execute(query="缓存查询", limit=5, **_hybrid)
        assert second.data.get("cached") is True
        assert [h["rank"] for h in second.data["hits"]] == list(range(1, 6))


class TestEnvKnobs:
    def test_env_int_defaults_and_overrides(self, monkeypatch):
        assert tools._env_int("SEARCH_TEST_UNSET", 7) == 7
        monkeypatch.setenv("SEARCH_TEST_UNSET", "9")
        assert tools._env_int("SEARCH_TEST_UNSET", 7) == 9
        monkeypatch.setenv("SEARCH_TEST_UNSET", "not-a-number")
        assert tools._env_int("SEARCH_TEST_UNSET", 7) == 7

    def test_manifest_declares_knob_defaults(self):
        from pathlib import Path

        import yaml

        manifest_path = (
            Path(__file__).resolve().parents[2] / "plugins" / "shared" / "search" / "plugin.yaml"
        )
        env = yaml.safe_load(manifest_path.read_text("utf-8"))["runtime"]["env"]
        for knob in (
            "SEARCH_KNN_K",
            "SEARCH_MAX_WINDOW",
            "SEARCH_NEIGHBOR_WINDOW",
            "SEARCH_CACHE_TTL_S",
            "SEARCH_CACHE_MAX_ENTRIES",
        ):
            assert knob in env


class TestCacheInvalidationNotification:
    """The host broadcasts search.cache_clear after reindexing; the plugin
    drops its coarse-result cache so fresh chunks are searchable at once."""

    @pytest.mark.asyncio
    async def test_cache_clear_notification_clears_cache(self):
        # In shared-process runs sys.modules["tools"] may be another
        # plugin's module; the handler is pinned to search's tools via
        # entry's module-level binding, so reach that namespace through it.
        from plugins.shared.search import entry as search_entry

        live = search_entry._cache_clear.__globals__

        plugin = SearchPlugin()
        plugin._setup_handlers()
        live["_cache_put"]("stale-key", {"hits": []})
        assert live["_cache_get"]("stale-key") is not None

        # Notification dispatch moved to per-connection state; no I/O needed here.
        from courtier_plugin_sdk.runtime import _Connection

        conn = _Connection(plugin, None, None, require_auth=False)
        await conn._process_line(json.dumps({"method": "search.cache_clear", "params": {}}))
        # The handler runs as a background task — give the loop a tick.
        await asyncio.sleep(0.01)
        assert live["_cache_get"]("stale-key") is None

    @pytest.mark.asyncio
    async def test_unknown_notification_is_ignored(self):
        plugin = SearchPlugin()
        plugin._setup_handlers()
        # Must not raise and must not disturb builtin handling.
        from courtier_plugin_sdk.runtime import _Connection

        conn = _Connection(plugin, None, None, require_auth=False)
        await conn._process_line(json.dumps({"method": "no.such.notification", "params": {}}))

    @pytest.mark.asyncio
    async def test_process_manager_notify_skips_inactive_plugins(self):
        from pathlib import Path
        from types import SimpleNamespace

        from courtier.plugin.manager import PluginState, ProcessManager
        from courtier.plugin.registry import ExtensionRegistry

        sent: list = []

        class FakeClient:
            async def notify(self, method, params):
                sent.append((method, params))

        pm = ProcessManager(Path("plugins"), ExtensionRegistry())
        proc = SimpleNamespace(state=PluginState.ACTIVE, _client=FakeClient())
        pm._processes["search"] = proc

        await pm.notify("search", "search.cache_clear", {})
        assert sent == [("search.cache_clear", {})]

        proc.state = PluginState.STOPPED
        await pm.notify("search", "search.cache_clear", {})
        assert len(sent) == 1  # inactive → skipped silently
