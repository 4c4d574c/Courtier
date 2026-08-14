"""Search plugin — Elasticsearch document chunk search."""

from courtier_plugin_sdk import PluginRuntime
from tools import SearchDocumentsTool


class SearchPlugin(PluginRuntime):
    def register_capabilities(self):
        return {
            "capabilities": [],
            "system_prompt": (
                "# 文档搜索\n\n"
                "## 能力\n通过自然语言关键词搜索已索引的文档块。\n"
                "## 使用方式\n调用 `search_documents` 工具，传入 `query`（搜索关键词）。\n\n"
                "## 查询改写（建议）\n"
                "检索前把用户问题改写为 1~3 个公文术语化查询（术语全称/简称、近义表述各一条），"
                "分别调用 `search_documents` 交叉验证，再综合多轮结果作答。\n"
            ),
        }

    def _setup_handlers(self):
        tool = SearchDocumentsTool()
        # register_tool() makes the tool available to the built-in dispatcher.
        self.register_tool(tool)


if __name__ == "__main__":
    import asyncio

    asyncio.run(SearchPlugin().run())
