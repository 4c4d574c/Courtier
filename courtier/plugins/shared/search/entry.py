"""Search plugin — Elasticsearch document chunk search."""

from courtier_plugin_sdk import PluginRuntime
from tools import ReadChunksTool, SearchDocumentsTool, _cache_clear


class SearchPlugin(PluginRuntime):
    def register_capabilities(self):
        # Tools are registered in _setup_handlers via register_tool(); the
        # model-facing usage/citation guidance lives in the core
        # behavioral.yaml prompt bundle (single source of truth — see
        # tests/courtier/test_prompt_single_source.py).
        return {"capabilities": [], "system_prompt": ""}

    def _setup_handlers(self):
        # register_tool() makes each tool available to the built-in dispatcher.
        self.register_tool(SearchDocumentsTool())
        self.register_tool(ReadChunksTool())

        # Module-level binding above keeps the handler pinned to this
        # plugin's tools module even when another plugin's ``tools`` gets
        # cached in sys.modules (shared-process test runs).
        @self.on_notification("search.cache_clear")
        def _on_cache_clear(params):
            # Host broadcast after reindexing: drop the coarse-result TTL
            # cache so fresh chunks are searchable immediately (the TTL
            # remains the fallback for missed notifications).
            _cache_clear()


if __name__ == "__main__":
    import asyncio

    asyncio.run(SearchPlugin().run())
