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
async def test_search_plugin_registers_with_system_prompt():
    plugin = SearchPlugin()
    plugin._setup_handlers()
    caps, system_prompt = plugin._collect_capabilities()
    tool_names = [c["name"] for c in caps if c["type"] == "tool"]
    assert "search_documents" in tool_names
    assert len(system_prompt) > 0


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
        body = tools._build_es_query("安全生产主体责任")
        mm = _multi_match(body)
        assert mm["operator"] == "and"
        assert "minimum_should_match" not in mm

    def test_multi_word_query_uses_minimum_should_match(self):
        body = tools._build_es_query("安全生产 主体责任 落实 煤矿")
        mm = _multi_match(body)
        assert mm["minimum_should_match"] == "70%"
        assert "operator" not in mm

    def test_long_unspaced_query_uses_minimum_should_match(self):
        body = tools._build_es_query("各单位应当落实安全生产主体责任并定期组织应急演练")
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


def _neighbor_hit(resource_id: int, chunk_no: int) -> dict:
    return {"resource_id": resource_id, "chunk_no": chunk_no, "chunk_text": "x", "title": "t"}


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
        ordered = await rerank.rerank_hits("查询", hits)
        assert [h["chunk_no"] for h in ordered] == [3, 1, 2]

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
            return reverse

        monkeypatch.setattr(rerank, "rerank_hits", fake_rerank)

        tool = tools.SearchDocumentsTool()
        result = await tool.execute(query="查询", rerank=True, limit=2)
        assert result.success
        assert result.data["reranked"] is True
        assert [h["chunk_no"] for h in result.data["hits"]] == [3, 2]
