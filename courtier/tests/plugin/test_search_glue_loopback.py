"""Loopback integration for the host-side search glue.

A real SDK plugin server exposes a search_documents tool with the
de-LLM contract (host-injected query_embedding + finalize pass); the
host dials it like any remote plugin and runs the real ToolRegistry
seams end-to-end: param injection → RPC → rerank post-processor →
finalize re-dispatch → RPC.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from courtier_plugin_sdk import PluginRuntime, ToolResult

import courtier.agent.runtime.search_rerank as rerank_mod
from courtier.agent.core.cache_store import CacheStore
from courtier.agent.runtime.search_rerank import make_search_rerank_post_processor
from courtier.agent.tools.param_injection import make_embedding_param_injector
from courtier.agent.tools.registry import ToolRegistry
from courtier.plugin import PluginSystem
from courtier.plugin.manager import PluginState

TOKEN = "integration-token"

_FAKE_SETTINGS = SimpleNamespace(
    llm_base_url="http://llm/v1",
    llm_api_key="k",
    llm_model="chat-model",
    llm_embedding_model="emb-model",
    search_rerank_fetch=100,
    search_rerank_candidate_budget_chars=24_000,
)


class _SearchGluePlugin(PluginRuntime):
    """Loopback stand-in for the search plugin's de-LLM contract."""

    def _setup_handlers(self):
        class SearchDocumentsTool:
            name = "search_documents"
            display_name = "搜索文档"
            description = "loopback search"
            parameters = {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "skip": {"type": "integer"},
                    "limit": {"type": "integer"},
                    "rerank": {"type": "boolean"},
                    "query_embedding": {
                        "type": "array",
                        "items": {"type": "number"},
                        "x-host-injected": "embedding",
                    },
                    "finalize": {
                        "type": "object",
                        "x-host-injected": "rerank-finalize",
                    },
                },
                "required": ["query"],
            }

            async def execute(self, **kwargs):
                if "finalize" in kwargs:
                    payload = kwargs["finalize"]
                    skip, limit = kwargs.get("skip", 0), kwargs.get("limit", 10)
                    return ToolResult(
                        success=True,
                        data={
                            **payload,
                            "hits": payload["hits"][skip : skip + limit],
                            "finalized": True,
                        },
                    )
                hits = [{"title": f"文档{i}", "chunk_no": i} for i in range(20)]
                mode = "hybrid" if kwargs.get("query_embedding") else "lexical"
                return ToolResult(success=True, data={"hits": hits, "total": 20, "mode": mode})

        self.register_tool(SearchDocumentsTool())


def _write_manifest(plugins_dir: Path) -> None:
    plugin_dir = plugins_dir / "search_glue"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        """name: search_glue
version: "0.1.0"
api: "2.0"
description: "Search glue loopback fixture"
""",
        encoding="utf-8",
    )


@pytest.fixture
def plugins_dir(tmp_path: Path) -> Path:
    d = tmp_path / "plugins"
    _write_manifest(d)
    return d


@pytest.fixture
async def search_server(monkeypatch):
    monkeypatch.setenv("COURTIER_PLUGIN_TOKEN", TOKEN)
    runtime = _SearchGluePlugin()
    task = asyncio.create_task(runtime.serve("127.0.0.1:0"))
    for _ in range(200):
        if runtime._server is not None and runtime._server.sockets:
            break
        await asyncio.sleep(0.01)
    else:  # pragma: no cover - defensive
        raise RuntimeError("plugin server did not start")
    port = runtime._server.sockets[0].getsockname()[1]
    yield port, task
    task.cancel()
    with contextlib.suppress(BaseException):
        await asyncio.wait_for(task, timeout=5)


async def _wait_active(ps: PluginSystem, timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        state = ps.get_status().get("search_glue", {}).get("state")
        if state == PluginState.ACTIVE.value:
            return
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"search_glue did not reach ACTIVE; last={state}")
        await asyncio.sleep(0.05)


def _make_system(plugins_dir: Path, port: int, tmp_path: Path):
    registry = ToolRegistry()
    registry.configure_param_injectors({"embedding": make_embedding_param_injector()})
    ps = PluginSystem(
        plugins_dir=plugins_dir,
        tool_registry=registry,
        artifact_store=CacheStore(cache_dir=str(tmp_path / "cache")),
        endpoints={"search_glue": ("127.0.0.1", port)},
        token=TOKEN,
    )
    return ps, registry


class _NoopEngine:
    def render(self, name, **variables):
        return ""


class TestSearchGlueLoopback:
    @pytest.mark.asyncio
    async def test_query_vector_injected_over_tcp(
        self, plugins_dir, search_server, tmp_path, monkeypatch
    ):
        port, _ = search_server
        monkeypatch.setattr("courtier.config.get_settings", lambda: _FAKE_SETTINGS)

        async def fake_embed_query(settings, text):
            assert text == "通知"
            return [0.1, 0.2, 0.3]

        monkeypatch.setattr("courtier.es.embeddings.embed_query", fake_embed_query)

        ps, registry = _make_system(plugins_dir, port, tmp_path)
        await ps.start()
        await _wait_active(ps)

        result = await registry.execute("search_documents", query="通知")
        assert result.success
        assert result.raw_data["mode"] == "hybrid"

        # Host-injected parameters are hidden from the model-visible schema.
        exposed = registry.get_schemas()[0]["function"]["parameters"]["properties"]
        assert "query_embedding" not in exposed
        assert "finalize" not in exposed
        assert "query" in exposed

        await ps.shutdown()

    @pytest.mark.asyncio
    async def test_no_injection_without_embedding_config(
        self, plugins_dir, search_server, tmp_path, monkeypatch
    ):
        port, _ = search_server
        disabled = SimpleNamespace(
            llm_base_url="http://llm/v1",
            llm_api_key="k",
            llm_model="chat-model",
            llm_embedding_model="",
            search_rerank_fetch=100,
            search_rerank_candidate_budget_chars=24_000,
        )
        monkeypatch.setattr("courtier.config.get_settings", lambda: disabled)

        ps, registry = _make_system(plugins_dir, port, tmp_path)
        await ps.start()
        await _wait_active(ps)

        result = await registry.execute("search_documents", query="通知")
        assert result.success
        assert result.raw_data["mode"] == "lexical"

        await ps.shutdown()

    @pytest.mark.asyncio
    async def test_rerank_finalize_roundtrip_over_tcp(
        self, plugins_dir, search_server, tmp_path, monkeypatch
    ):
        port, _ = search_server
        monkeypatch.setattr("courtier.config.get_settings", lambda: _FAKE_SETTINGS)

        async def reverse_rerank(query, hits, *, settings, prompt_engine):
            return list(reversed(hits)), False

        monkeypatch.setattr(rerank_mod, "rerank_hits", reverse_rerank)

        ps, registry = _make_system(plugins_dir, port, tmp_path)
        registry.configure_result_post_processors(
            [make_search_rerank_post_processor(_NoopEngine(), registry)]
        )
        await ps.start()
        await _wait_active(ps)

        result = await registry.execute("search_documents", query="通知", rerank=True, limit=3)
        assert result.success
        # Reversed coarse hits, then paginated by the finalize pass.
        assert [h["chunk_no"] for h in result.raw_data["hits"]] == [19, 18, 17]
        assert result.raw_data["reranked"] is True
        assert result.raw_data["rerank_partial"] is False
        assert result.raw_data["finalized"] is True
        assert [h.get("rank") for h in result.raw_data["hits"]] == [None, None, None]

        await ps.shutdown()

    @pytest.mark.asyncio
    async def test_rerank_failure_keeps_order_over_tcp(
        self, plugins_dir, search_server, tmp_path, monkeypatch
    ):
        port, _ = search_server
        monkeypatch.setattr("courtier.config.get_settings", lambda: _FAKE_SETTINGS)

        async def failing_rerank(query, hits, *, settings, prompt_engine):
            raise RuntimeError("model down")

        monkeypatch.setattr(rerank_mod, "rerank_hits", failing_rerank)

        ps, registry = _make_system(plugins_dir, port, tmp_path)
        registry.configure_result_post_processors(
            [make_search_rerank_post_processor(_NoopEngine(), registry)]
        )
        await ps.start()
        await _wait_active(ps)

        result = await registry.execute("search_documents", query="通知", rerank=True, limit=3)
        assert result.success
        assert [h["chunk_no"] for h in result.raw_data["hits"]] == [0, 1, 2]
        assert result.raw_data["reranked"] is False
        assert result.raw_data["rerank_partial"] is True
        assert result.raw_data["finalized"] is True

        await ps.shutdown()
