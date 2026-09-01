"""Parse plugin — document layout parsing."""

from courtier_plugin_sdk import PluginRuntime
from tools import ParseTool


class ParsePlugin(PluginRuntime):
    def register_capabilities(self):
        # system_prompt is sent to the host in the register notification and
        # ends up in the agent's system prompt — entry.py is its single
        # source of truth (plugin.yaml intentionally does not repeat it).
        return {
            "capabilities": [],
            "system_prompt": (
                "# 格式解析\n\n"
                "## 能力\n"
                "将 PDF、DOCX、扫描件/图片解析为结构化的 Document 模型，\n"
                "提取版面结构（页眉/页脚/正文块、字体、字号、间距）。\n"
                "DOCX 与文本型 PDF 由规则引擎解析，结果是确定性的；\n"
                "扫描件与图片需逐页 OCR 并调用多模态 LLM 识别结构，耗时长且存在识别误差。\n"
                "## 使用方式\n"
                "调用 `parse_layout` 工具，传入 `file_path` 参数（绝对路径）。\n"
                "返回结果中的 `warnings` 字段（如有）说明解析出现降级，后续环节需留意。\n"
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
