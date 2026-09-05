"""Memory plugin — layered DB-backed memory (global + per-user)."""

from courtier_plugin_sdk import PluginRuntime
from tools import MemoryTool


class MemoryPlugin(PluginRuntime):
    def register_capabilities(self):
        return {
            "capabilities": [],
            "system_prompt": (
                "# 长期记忆\n\n"
                "## 能力\n"
                "通过 memory 工具维护跨会话记忆：用户层（个人，仅本人可见）与"
                "全局共享层（团队/领域知识，仅管理员可写）。条目分通用（common）"
                "与领域包两类。\n\n"
                "## 约定\n"
                "- 用户表达偏好（语言、风格、常用口径等）时，主动写入用户层"
                "（action=write，layer=user，domain=common）。\n"
                "- 领域相关的口径与规则（如某领域的审核规则、验收标准、行业规范）"
                "写入对应领域包：action=write，domain=<领域包名>。可用领域包见"
                "「已加载领域包」或 memory list 结果里的 known_domains。\n"
                "- 写入前先 list/read 查重，已有相近条目则更新而不是另建。\n"
                "- 标题稳定且具体（如「回复语言偏好」），便于后续按 title 寻址。\n"
                "- 用户要求「记住/忘记」时用本工具落实，并简短确认结果。"
            ),
        }

    def _setup_handlers(self):
        tool = MemoryTool(
            host_client_getter=lambda: self.host_service_client,
        )
        # register_tool() makes the tool available to the built-in dispatcher.
        self.register_tool(tool)


if __name__ == "__main__":
    import asyncio

    asyncio.run(MemoryPlugin().run())
