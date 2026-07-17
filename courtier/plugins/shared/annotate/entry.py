"""Annotate plugin — DOCX keyword-based annotation."""
from tools import AnnotateDocumentTool

from courtier.plugin.sdk import PluginRuntime


class AnnotatePlugin(PluginRuntime):
    def register_capabilities(self):
        return {
            "capabilities": [],
            "system_prompt": (
                "# 文档批注\n\n## 能力\n"
                "在 DOCX 文档中根据关键词自动添加 Word 批注（comments）。\n"
            ),
        }

    def _setup_handlers(self):
        tool = AnnotateDocumentTool()
        # register_tool() makes the tool available to the built-in dispatcher.
        self.register_tool(tool)


if __name__ == "__main__":
    import asyncio
    asyncio.run(AnnotatePlugin().run())
