"""Plagiarism plugin — similarity-based plagiarism detection."""

from tools import DetectPlagiarismTool

from courtier.plugin.sdk import PluginRuntime


class PlagiarismPlugin(PluginRuntime):
    def register_capabilities(self):
        return {
            "system_prompt": (
                "# 查重检测\n\n## 流水线步骤\n"
                "调用 detect_plagiarism 完成查重"
                "（内部会逐段计算相似度、动态阈值计算和抄袭判定）。\n"
            ),
        }

    def _setup_handlers(self):
        detect = DetectPlagiarismTool()
        self.register_tool(detect)


if __name__ == "__main__":
    import asyncio

    asyncio.run(PlagiarismPlugin().run())
