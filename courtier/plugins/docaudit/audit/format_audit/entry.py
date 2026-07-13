"""Format audit plugin — validates document formatting against rule templates."""

from tools import FormatAuditTool

from courtier.plugin.sdk import PluginRuntime


class FormatAuditPlugin(PluginRuntime):
    def register_capabilities(self):
        return {
            "system_prompt": "# 格式审核\n\n检查文档格式是否符合 GB/T 9704-2012 标准。\n",
        }

    def _setup_handlers(self):
        tool = FormatAuditTool()
        self.register_tool(tool)


if __name__ == "__main__":
    import asyncio

    asyncio.run(FormatAuditPlugin().run())
