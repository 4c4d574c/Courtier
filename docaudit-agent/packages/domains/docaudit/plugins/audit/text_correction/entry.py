"""Text correction plugin — detects and corrects Chinese text errors."""

from tools import TextCorrectionTool

from courtier.plugin.sdk import PluginRuntime


class TextCorrectionPlugin(PluginRuntime):
    def register_capabilities(self):
        return {
            "system_prompt": (
                "# 文本纠错\n\n"
                "检测并纠正中文文本中的拼写、语法、全角半角混用和标点错误。\n"
            ),
        }

    def _setup_handlers(self):
        tool = TextCorrectionTool()
        self.register_tool(tool)


if __name__ == "__main__":
    import asyncio

    asyncio.run(TextCorrectionPlugin().run())
