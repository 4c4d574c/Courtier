"""Search plugin — Elasticsearch document chunk search."""

from courtier_plugin_sdk import PluginRuntime
from tools import ReadChunksTool, SearchDocumentsTool


class SearchPlugin(PluginRuntime):
    def register_capabilities(self):
        # Tools are registered in _setup_handlers via register_tool(); the
        # model-facing usage/citation guidance lives in the core
        # behavioral.yaml prompt bundle (single source of truth — see
        # tests/courtier/test_prompt_single_source.py).
        return []

    def _setup_handlers(self):
        # register_tool() makes each tool available to the built-in dispatcher.
        self.register_tool(SearchDocumentsTool())
        self.register_tool(ReadChunksTool())


if __name__ == "__main__":
    import asyncio

    asyncio.run(SearchPlugin().run())
