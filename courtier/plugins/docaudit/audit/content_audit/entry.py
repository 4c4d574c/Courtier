"""Content audit plugin — validates document body text against content compliance rules."""

from courtier_plugin_sdk import PluginRuntime
from tools import ContentAuditTool


class ContentAuditPlugin(PluginRuntime):
    def register_capabilities(self):
        return {
            "system_prompt": "# 内容合规审核\n\n检查公文正文内容是否符合文种规范。\n",
        }

    def _setup_handlers(self):
        tool = ContentAuditTool()
        self.register_tool(tool)


if __name__ == "__main__":
    import asyncio

    asyncio.run(ContentAuditPlugin().run())
