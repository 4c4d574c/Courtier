"""Template plugin — database-backed template loading."""
from tools import LoadTemplateTool

from courtier.plugin.sdk import PluginRuntime


class TemplatePlugin(PluginRuntime):
    def register_capabilities(self):
        return {
            "capabilities": [],
            "system_prompt": "# 模板加载\n\n## 能力\n从数据库按文种加载格式模板。\n",
        }

    def _setup_handlers(self):
        tool = LoadTemplateTool()
        # register_tool() makes the tool available to the built-in dispatcher.
        self.register_tool(tool)


if __name__ == "__main__":
    import asyncio
    asyncio.run(TemplatePlugin().run())
