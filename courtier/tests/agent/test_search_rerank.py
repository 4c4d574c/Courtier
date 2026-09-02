"""Host-side search rerank: order parsing, prompt budget, and the
registry post-processor (rerank → finalize re-dispatch) semantics."""

from types import SimpleNamespace

import pytest

import courtier.agent.runtime.search_rerank as rerank_mod
from courtier.agent.runtime.search_rerank import (
    _best_window,
    _candidate_lines,
    _parse_order,
    make_search_rerank_post_processor,
    rerank_hits,
)
from courtier.agent.tools.protocol import ToolResult
from courtier.agent.tools.registry import ToolRegistry

COARSE_HITS = [
    {"title": f"文档{i}", "chunk_text": f"第{i}号文的相关内容。" * 10} for i in range(1, 4)
]


def _settings(**overrides):
    base = {
        "llm_base_url": "http://llm/v1",
        "llm_api_key": "k",
        "llm_model": "chat-model",
        "search_rerank_fetch": 3,
        "search_rerank_candidate_budget_chars": 24_000,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class _FakeEngine:
    def render(self, name, **variables):
        if name == "search.rerank.system":
            return "SYSTEM"
        return f"USER {variables.get('query', '')} {variables.get('candidates', '')}"


class _FakeBackend:
    """Captures the chat request and returns a canned completion."""

    last_request = None
    last_kwargs: dict = {}
    response_content = "[2, 1, 3]"
    raise_on_call: Exception | None = None

    def __init__(self, **kwargs):
        _FakeBackend.last_kwargs = dict(kwargs)

    async def chat(self, request):
        if _FakeBackend.raise_on_call:
            raise _FakeBackend.raise_on_call
        _FakeBackend.last_request = request
        return SimpleNamespace(message=SimpleNamespace(content=_FakeBackend.response_content))

    async def close(self):
        pass


@pytest.fixture(autouse=True)
def _fake_backend():
    _FakeBackend.last_request = None
    _FakeBackend.last_kwargs = {}
    _FakeBackend.response_content = "[2, 1, 3]"
    _FakeBackend.raise_on_call = None
    yield


class TestParseOrder:
    def test_plain_json(self):
        assert _parse_order("[3, 1, 2]", 3) == [3, 1, 2]

    def test_prose_wrapped(self):
        assert _parse_order("排序结果：[2, 1, 3] 以上。", 3) == [2, 1, 3]

    def test_invalid_returns_none(self):
        assert _parse_order("无法排序", 3) is None
        assert _parse_order("[9]", 3) is None
        assert _parse_order("[]", 3) is None

    def test_out_of_range_and_duplicates_filtered(self):
        assert _parse_order("[1, 9, 1, 2]", 3) == [1, 2]


class TestCandidateBudget:
    def test_best_window_centers_on_matching_sentence(self):
        text = "无关内容。" * 50 + "这里是与查询最相关的句子。" + "尾部无关。" * 50
        window = _best_window({"查询", "相关"}, text, 40)
        assert "相关" in window
        assert len(window) <= 42  # 40 + ellipses

    def test_best_window_falls_back_to_head(self):
        text = "abcdefgh" * 100
        window = _best_window(set(), text, 20)
        assert window == text[:20]

    def test_candidate_lines_numbered_and_quoted(self):
        lines = _candidate_lines("查询", COARSE_HITS, 24_000)
        assert len(lines) == 3
        assert lines[0].startswith("1. 《文档1》 ")


class TestRerankHits:
    async def test_success_reorders(self, monkeypatch):
        monkeypatch.setattr(rerank_mod, "OpenAIModelBackend", _FakeBackend)
        ordered, partial = await rerank_hits(
            "查询", COARSE_HITS, settings=_settings(), prompt_engine=_FakeEngine()
        )
        assert [h["title"] for h in ordered] == ["文档2", "文档1", "文档3"]
        assert partial is False

    async def test_partial_coverage_appends_remainder(self, monkeypatch):
        monkeypatch.setattr(rerank_mod, "OpenAIModelBackend", _FakeBackend)
        _FakeBackend.response_content = "[1, 2]"
        ordered, partial = await rerank_hits(
            "查询", COARSE_HITS, settings=_settings(), prompt_engine=_FakeEngine()
        )
        assert [h["title"] for h in ordered] == ["文档1", "文档2", "文档3"]
        assert partial is True

    async def test_low_coverage_keeps_original_order(self, monkeypatch):
        monkeypatch.setattr(rerank_mod, "OpenAIModelBackend", _FakeBackend)
        _FakeBackend.response_content = "[3]"
        ordered, partial = await rerank_hits(
            "查询", COARSE_HITS, settings=_settings(), prompt_engine=_FakeEngine()
        )
        assert [h["title"] for h in ordered] == ["文档1", "文档2", "文档3"]
        assert partial is True

    async def test_unconfigured_raises(self):
        settings = _settings(llm_base_url="")
        with pytest.raises(RuntimeError, match="not configured"):
            await rerank_hits("查询", COARSE_HITS, settings=settings, prompt_engine=_FakeEngine())

    async def test_unparsable_output_raises(self, monkeypatch):
        monkeypatch.setattr(rerank_mod, "OpenAIModelBackend", _FakeBackend)
        _FakeBackend.response_content = "抱歉，我无法"
        with pytest.raises(RuntimeError, match="unparsable"):
            await rerank_hits(
                "查询", COARSE_HITS, settings=_settings(), prompt_engine=_FakeEngine()
            )

    async def test_single_hit_short_circuits(self, monkeypatch):
        monkeypatch.setattr(rerank_mod, "OpenAIModelBackend", _FakeBackend)
        ordered, partial = await rerank_hits(
            "查询", COARSE_HITS[:1], settings=_settings(), prompt_engine=_FakeEngine()
        )
        assert ordered == COARSE_HITS[:1]
        assert partial is False
        assert _FakeBackend.last_request is None


class TestProfileFollow:
    """rerank 跟随本次运行所选的模型档案（聊天链跟随不变式）。"""

    async def test_rerank_uses_profile_endpoint(self, monkeypatch):
        monkeypatch.setattr(rerank_mod, "OpenAIModelBackend", _FakeBackend)
        profile = SimpleNamespace(base_url="http://pool/v1", api_key="sk-pool", model="pool-model")
        await rerank_hits(
            "查询",
            COARSE_HITS,
            settings=_settings(),
            prompt_engine=_FakeEngine(),
            profile=profile,
        )
        assert _FakeBackend.last_kwargs["base_url"] == "http://pool/v1"
        assert _FakeBackend.last_kwargs["api_key"] == "sk-pool"
        assert _FakeBackend.last_kwargs["model"] == "pool-model"
        assert _FakeBackend.last_request.model == "pool-model"

    async def test_profile_applies_even_with_unconfigured_scalars(self, monkeypatch):
        monkeypatch.setattr(rerank_mod, "OpenAIModelBackend", _FakeBackend)
        profile = SimpleNamespace(base_url="http://pool/v1", api_key="", model="pool-model")
        ordered, _partial = await rerank_hits(
            "查询",
            COARSE_HITS,
            settings=_settings(llm_base_url="", llm_api_key="", llm_model=""),
            prompt_engine=_FakeEngine(),
            profile=profile,
        )
        assert [h["title"] for h in ordered] == ["文档2", "文档1", "文档3"]

    async def test_no_profile_keeps_scalar_path(self, monkeypatch):
        monkeypatch.setattr(rerank_mod, "OpenAIModelBackend", _FakeBackend)
        await rerank_hits("查询", COARSE_HITS, settings=_settings(), prompt_engine=_FakeEngine())
        assert _FakeBackend.last_kwargs["base_url"] == "http://llm/v1"
        assert _FakeBackend.last_kwargs["model"] == "chat-model"

    async def test_post_processor_reads_run_context_profile(self, monkeypatch):
        from courtier.agent.runtime.run_context import run_model_profile

        registry = ToolRegistry()
        tool = _FinalizeSearchTool()
        registry.register(tool)
        monkeypatch.setattr("courtier.config.get_settings", lambda: _settings())
        monkeypatch.setattr(rerank_mod, "OpenAIModelBackend", _FakeBackend)
        processor = make_search_rerank_post_processor(_FakeEngine(), registry)

        profile = SimpleNamespace(base_url="http://pool/v1", api_key="sk-run", model="run-model")
        token = run_model_profile.set(profile)
        try:
            original = ToolResult(success=True, data={"hits": [dict(h) for h in COARSE_HITS]})
            await processor("search_documents", {"query": "q", "rerank": True}, original)
        finally:
            run_model_profile.reset(token)

        assert _FakeBackend.last_kwargs["base_url"] == "http://pool/v1"
        assert _FakeBackend.last_kwargs["api_key"] == "sk-run"
        assert tool.calls[0]["finalize"]["reranked"] is True


class _FinalizeSearchTool:
    """Search tool stub: coarse search + finalize mode."""

    def __init__(self):
        self.name = "search_documents"
        self.description = "search"
        self.parameters = {"type": "object", "properties": {"query": {"type": "string"}}}
        self.calls: list[dict] = []

    async def execute(self, **kwargs):
        self.calls.append(kwargs)
        if "finalize" in kwargs:
            payload = kwargs["finalize"]
            return ToolResult(
                success=True, data={**payload, "finalized": True, "skip": kwargs.get("skip")}
            )
        return ToolResult(
            success=True,
            data={"hits": [dict(h) for h in COARSE_HITS], "total": 3, "query": kwargs.get("query")},
        )


class TestPostProcessor:
    def _registry(self):
        registry = ToolRegistry()
        tool = _FinalizeSearchTool()
        registry.register(tool)
        return registry, tool

    async def test_ignores_other_tools_and_flags(self, monkeypatch):
        monkeypatch.setattr("courtier.config.get_settings", lambda: _settings())
        processor = make_search_rerank_post_processor(_FakeEngine())
        result = ToolResult(success=True, data={"hits": COARSE_HITS})

        failed = ToolResult(success=False, error="x")
        bad_shape = ToolResult(success=True, data="not-a-dict")
        assert await processor("read_chunks", {"rerank": True}, result) is None
        assert await processor("search_documents", {"rerank": False}, result) is None
        assert await processor("search_documents", {"rerank": True}, failed) is None
        assert await processor("search_documents", {"rerank": True}, bad_shape) is None

    async def test_finalize_receives_reordered_hits(self, monkeypatch):
        registry, tool = self._registry()
        monkeypatch.setattr("courtier.config.get_settings", lambda: _settings())
        monkeypatch.setattr(rerank_mod, "OpenAIModelBackend", _FakeBackend)
        processor = make_search_rerank_post_processor(_FakeEngine(), registry)

        original = ToolResult(success=True, data={"hits": [dict(h) for h in COARSE_HITS]})
        kwargs = {"query": "q", "rerank": True, "skip": 1, "limit": 2}
        final = await processor("search_documents", kwargs, original)

        # Finalize was dispatched once with the reordered coarse payload.
        assert len(tool.calls) == 1
        finalize_call = tool.calls[0]
        finalize_hits = finalize_call["finalize"]["hits"]
        assert [h["title"] for h in finalize_hits] == ["文档2", "文档1", "文档3"]
        assert finalize_call["finalize"]["reranked"] is True
        assert finalize_call["skip"] == 1 and finalize_call["limit"] == 2
        # The finalized result replaces the coarse one (persistence sees it).
        assert final.data["finalized"] is True
        assert final.data["skip"] == 1

    async def test_rerank_failure_keeps_order_but_still_finalizes(self, monkeypatch):
        registry, tool = self._registry()
        monkeypatch.setattr("courtier.config.get_settings", lambda: _settings())
        _FakeBackend.raise_on_call = RuntimeError("model down")
        monkeypatch.setattr(rerank_mod, "OpenAIModelBackend", _FakeBackend)
        processor = make_search_rerank_post_processor(_FakeEngine(), registry)

        original = ToolResult(success=True, data={"hits": [dict(h) for h in COARSE_HITS]})
        final = await processor("search_documents", {"query": "q", "rerank": True}, original)

        assert final.data["finalized"] is True
        ordered_titles = [h["title"] for h in tool.calls[0]["finalize"]["hits"]]
        assert ordered_titles == ["文档1", "文档2", "文档3"]
        assert tool.calls[0]["finalize"]["reranked"] is False
        assert tool.calls[0]["finalize"]["rerank_partial"] is True

    async def test_finalize_failure_returns_coarse_reranked(self, monkeypatch):
        registry, tool = self._registry()
        monkeypatch.setattr("courtier.config.get_settings", lambda: _settings())
        monkeypatch.setattr(rerank_mod, "OpenAIModelBackend", _FakeBackend)

        async def broken_finalize(**kwargs):
            raise RuntimeError("plugin unreachable")

        tool.execute = broken_finalize
        processor = make_search_rerank_post_processor(_FakeEngine(), registry)

        original = ToolResult(success=True, data={"hits": [dict(h) for h in COARSE_HITS]})
        final = await processor("search_documents", {"query": "q", "rerank": True}, original)

        assert [h["title"] for h in final.data["hits"]] == ["文档2", "文档1", "文档3"]
        assert final.data["reranked"] is True

    async def test_without_registry_returns_flagged_payload(self, monkeypatch):
        monkeypatch.setattr("courtier.config.get_settings", lambda: _settings())
        monkeypatch.setattr(rerank_mod, "OpenAIModelBackend", _FakeBackend)
        processor = make_search_rerank_post_processor(_FakeEngine())

        original = ToolResult(success=True, data={"hits": [dict(h) for h in COARSE_HITS]})
        final = await processor("search_documents", {"query": "q", "rerank": True}, original)
        assert [h["title"] for h in final.data["hits"]] == ["文档2", "文档1", "文档3"]
        assert final.data["reranked"] is True

    async def test_respects_fetch_limit(self, monkeypatch):
        registry, tool = self._registry()
        monkeypatch.setattr(
            "courtier.config.get_settings", lambda: _settings(search_rerank_fetch=1)
        )
        monkeypatch.setattr(rerank_mod, "OpenAIModelBackend", _FakeBackend)
        processor = make_search_rerank_post_processor(_FakeEngine(), registry)

        original = ToolResult(success=True, data={"hits": [dict(h) for h in COARSE_HITS]})
        await processor("search_documents", {"query": "q", "rerank": True}, original)

        finalize_hits = tool.calls[0]["finalize"]["hits"]
        # Only the first candidate went through the model; the rest keep
        # their original relative order after it.
        assert [h["title"] for h in finalize_hits[1:]] == ["文档2", "文档3"]


class TestTemplates:
    def test_rerank_templates_in_core_defaults(self):
        from courtier.prompts.engine import _load_core_defaults

        for locale in ("zh-CN", "en-US"):
            bundle = _load_core_defaults(locale)
            assert "search.rerank.system" in bundle.templates, locale
            assert "search.rerank.user" in bundle.templates, locale

    def test_rerank_templates_render_via_engine(self):
        from courtier.prompts.engine import PromptEngine

        engine = PromptEngine.from_domain_directories([], locale="zh-CN")
        system = engine.render("search.rerank.system")
        user = engine.render("search.rerank.user", query="通知", candidates="1. 《文档》 内容")
        # RESERVED_TEMPLATE_KEYS must include the keys, or the engine
        # silently drops the yaml bundle and falls back to English.
        assert "重排器" in system
        assert "通知" in user and "《文档》" in user

    def test_settings_defaults(self):
        from courtier.config import Settings

        settings = Settings()
        assert settings.search_rerank_fetch == 100
        assert settings.search_rerank_candidate_budget_chars == 24_000
