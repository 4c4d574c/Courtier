"""Parse plugin — document file parsing."""
from courtier.plugin.sdk import PluginRuntime
from tools import ParseTool


class ParsePlugin(PluginRuntime):
    def register_capabilities(self):
        return {
            "capabilities": [],
            "system_prompt": (
                "# 文档解析\n\n"
                "## 能力\n将 PDF、DOCX、扫描图片解析为结构化的 Document 模型。\n"
                "## 使用方式\n调用 `parse_document` 工具，传入 `file_path` 参数（绝对路径）。\n"
            ),
        }

    def _setup_handlers(self):
        tool = ParseTool()
        # register_tool() makes the tool available to the built-in
        # tool.execute dispatcher — no manual dispatch needed.
        self.register_tool(tool)


if __name__ == "__main__":
    import asyncio
    asyncio.run(ParsePlugin().run())
