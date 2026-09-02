"""Host-injected tool parameters: boundary injection + schema stripping.

A tool contract marks a property with ``"x-host-injected": "<injector>"``;
the ToolRegistry fills it at dispatch (after $ref resolution, before the
tool runs) and hides it from the model-visible schema.
"""

from courtier.agent.tools.param_injection import (
    HOST_INJECTED_MARKER,
    embedding_injector,
    make_embedding_param_injector,
)
from courtier.agent.tools.protocol import ToolResult
from courtier.agent.tools.registry import ToolRegistry


class _FakeTool:
    """Minimal tool recording the kwargs it was executed with."""

    def __init__(self, parameters=None):
        self.name = "search_documents"
        self.description = "hybrid search"
        self.parameters = parameters or {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "rerank": {"type": "boolean"},
                "query_embedding": {
                    "type": "array",
                    "items": {"type": "number"},
                    HOST_INJECTED_MARKER: "embedding",
                },
            },
            "required": ["query"],
        }
        self.executed_kwargs: dict | None = None

    async def execute(self, **kwargs):
        self.executed_kwargs = kwargs
        return ToolResult(success=True, data={"hits": []})


class TestParamInjection:
    async def test_injector_fills_param_before_execute(self):
        registry = ToolRegistry()
        tool = _FakeTool()
        registry.register(tool)
        registry.configure_param_injectors({"embedding": lambda t, kw, p: [0.1, 0.2]})

        result = await registry.execute("search_documents", query="公文格式")

        assert result.success
        assert tool.executed_kwargs["query_embedding"] == [0.1, 0.2]
        assert tool.executed_kwargs["query"] == "公文格式"

    async def test_async_injector_awaited(self):
        registry = ToolRegistry()
        tool = _FakeTool()
        registry.register(tool)

        async def injector(tool, kwargs, param_name):
            return [0.5]

        registry.configure_param_injectors({"embedding": injector})
        await registry.execute("search_documents", query="q")
        assert tool.executed_kwargs["query_embedding"] == [0.5]

    async def test_no_injector_configured_leaves_param_unset(self):
        registry = ToolRegistry()
        tool = _FakeTool()
        registry.register(tool)
        # No configure_param_injectors call at all.
        await registry.execute("search_documents", query="q")
        assert "query_embedding" not in tool.executed_kwargs

    async def test_unknown_injector_name_leaves_param_unset(self):
        registry = ToolRegistry()
        tool = _FakeTool()
        registry.register(tool)
        registry.configure_param_injectors({"other": lambda *a: [1.0]})
        await registry.execute("search_documents", query="q")
        assert "query_embedding" not in tool.executed_kwargs

    async def test_failing_injector_only_skips_its_param(self):
        registry = ToolRegistry()
        tool = _FakeTool()
        registry.register(tool)

        def boom(tool, kwargs, param_name):
            raise RuntimeError("injector down")

        registry.configure_param_injectors({"embedding": boom})
        result = await registry.execute("search_documents", query="q")
        assert result.success
        assert "query_embedding" not in tool.executed_kwargs

    async def test_none_return_leaves_param_unset(self):
        registry = ToolRegistry()
        tool = _FakeTool()
        registry.register(tool)
        registry.configure_param_injectors({"embedding": lambda *a: None})
        await registry.execute("search_documents", query="q")
        assert "query_embedding" not in tool.executed_kwargs

    async def test_explicit_kwarg_not_overwritten(self):
        registry = ToolRegistry()
        tool = _FakeTool()
        registry.register(tool)
        registry.configure_param_injectors({"embedding": lambda *a: [9.9]})
        await registry.execute("search_documents", query="q", query_embedding=[1.0])
        assert tool.executed_kwargs["query_embedding"] == [1.0]

    async def test_marker_params_hidden_from_model_schemas(self):
        registry = ToolRegistry()
        tool = _FakeTool()
        registry.register(tool)

        schemas = registry.get_schemas()
        parameters = schemas[0]["function"]["parameters"]

        assert "query_embedding" not in parameters["properties"]
        assert "query" in parameters["properties"]
        assert parameters["required"] == ["query"]
        # The tool's own schema is untouched (registry copies, never mutates).
        assert HOST_INJECTED_MARKER in tool.parameters["properties"]["query_embedding"]

    async def test_hidden_required_entry_dropped_from_schema(self):
        parameters = {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "query_embedding": {HOST_INJECTED_MARKER: "embedding"},
            },
            "required": ["query", "query_embedding"],
        }
        registry = ToolRegistry()
        registry.register(_FakeTool(parameters))

        exposed = registry.get_schemas()[0]["function"]["parameters"]
        assert "query_embedding" not in exposed["properties"]
        assert exposed["required"] == ["query"]

    async def test_clone_carries_param_injectors(self):
        registry = ToolRegistry()
        registry.register(_FakeTool())
        registry.configure_param_injectors({"embedding": lambda *a: [0.3]})

        clone = registry.clone()
        tool = clone.get("search_documents")
        await clone.execute("search_documents", query="q")
        assert tool.executed_kwargs["query_embedding"] == [0.3]


class _FakeSettings:
    llm_base_url = ""
    llm_api_key = ""
    llm_embedding_model = ""


class TestEmbeddingInjector:
    async def test_disabled_embedding_returns_none(self, monkeypatch):
        monkeypatch.setattr("courtier.config.get_settings", lambda: _FakeSettings())
        tool = _FakeTool()
        assert await embedding_injector(tool, {"query": "q"}, "query_embedding") is None

    async def test_blank_query_returns_none(self, monkeypatch):
        settings = _FakeSettings()
        settings.llm_embedding_base_url = "http://llm"
        settings.llm_embedding_model = "emb"
        monkeypatch.setattr("courtier.config.get_settings", lambda: settings)

        tool = _FakeTool()
        assert await embedding_injector(tool, {"query": "  "}, "query_embedding") is None

    async def test_embeds_query_via_host_client(self, monkeypatch):
        settings = _FakeSettings()
        settings.llm_embedding_base_url = "http://llm"
        settings.llm_embedding_model = "emb"
        monkeypatch.setattr("courtier.config.get_settings", lambda: settings)

        async def fake_embed_query(s, text):
            assert text == "公文 格式"
            return [0.1, 0.2]

        monkeypatch.setattr("courtier.es.embeddings.embed_query", fake_embed_query)
        tool = _FakeTool()
        vector = await embedding_injector(tool, {"query": "公文 格式"}, "query_embedding")
        assert vector == [0.1, 0.2]

    async def test_make_embedding_param_injector_wraps_function(self):
        assert make_embedding_param_injector() is embedding_injector
