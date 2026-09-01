"""anydoc plugin — generic office document conversion to Markdown."""

from courtier_plugin_sdk import PluginRuntime
from tools import ConvertDocumentTool


class AnyDocPlugin(PluginRuntime):
    def register_capabilities(self):
        # system_prompt is sent to the host in the register notification and
        # ends up in the agent's system prompt — entry.py is its single
        # source of truth (plugin.yaml intentionally does not repeat it).
        return {
            "capabilities": [],
            "system_prompt": (
                "# 文档转换\n\n"
                "## 能力\n"
                "将 Word、Excel、PowerPoint、OpenDocument、RTF、EPUB、CSV、文本型 PDF\n"
                "转换为 GitHub Flavored Markdown，保留标题、列表、表格等结构。\n"
                "扫描件/图片型 PDF 与图片文件会走 OCR 兜底（需配置 OCR 服务），\n"
                "输出按阅读顺序拼接的 Markdown。\n"
                "## 限制\n"
                "不提供版面格式信息；需要公文版面结构（GB/T 9704）时改用\n"
                "parse_layout（公文审计流程）。\n"
                "## 使用方式\n"
                "调用 `convert_document` 工具，传入 `file_path` 参数（绝对路径），\n"
                "返回结果中的 `markdown` 字段即为文档内容。\n"
            ),
        }

    def _setup_handlers(self):
        tool = ConvertDocumentTool()
        # register_tool() makes the tool available to the built-in
        # tool.execute dispatcher — no manual dispatch needed.
        self.register_tool(tool)


if __name__ == "__main__":
    import asyncio

    asyncio.run(AnyDocPlugin().run())
