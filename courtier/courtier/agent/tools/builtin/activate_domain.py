"""ActivateDomainTool — orchestrator meta-tool for domain self-activation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..protocol import ToolResult

if TYPE_CHECKING:
    from courtier.agent.runtime.activation import DomainActivator


class ActivateDomainTool:
    """激活一个领域包，使该领域的专用工具与技能对本会话可见（域门控）。

    领域目录（名称+描述）在构造时静态嵌入工具描述，模型无需额外查询即可
    决定是否激活。激活是幂等的：重复激活同一领域直接返回成功。
    """

    name: str = "activate_domain"
    display_name: str | None = "激活领域"
    skip_ref_resolution: bool = True
    skip_persist: bool = True
    skip_summarize: bool = True
    runtime_policy = None  # orchestrator-level meta-tool: no call budget

    def __init__(self, domain_catalog: list[dict[str, str]]) -> None:
        catalog_lines = [f"- **{item['name']}**：{item['description']}" for item in domain_catalog]
        catalog_text = "\n".join(catalog_lines) or "（无可用领域）"
        self.description = (
            "激活一个领域包，使该领域的专用工具与技能对当前会话可见。\n"
            "当用户意图明显命中某个领域（例如公文格式/内容审核）时，"
            "先调用本工具完成激活，再使用该领域提供的工具/技能；"
            "「疑似即激活」——意图模糊但可能涉及领域能力时也应激活，"
            "重复激活无副作用。\n"
            f"可用领域：\n{catalog_text}"
        )
        self.parameters: dict[str, Any] = {
            "type": "object",
            "properties": {
                "domain": {
                    "type": "string",
                    "description": "要激活的领域名称（见描述中的可用领域列表）。",
                }
            },
            "required": ["domain"],
        }
        self._activator: DomainActivator | None = None

    def set_activator(self, activator: "DomainActivator | None") -> None:
        """Wire the per-orchestrator activator (host-side, not in the schema)."""
        self._activator = activator

    async def execute(self, domain: str | None = None, **kwargs: Any) -> ToolResult:
        if self._activator is None:
            return ToolResult(
                success=False,
                error="激活器未接线：activate_domain 需要在编排器构建时绑定 DomainActivator",
            )
        if not isinstance(domain, str) or not domain:
            return ToolResult(
                success=False,
                error="缺少必填参数 domain（领域名称，见工具描述中的可用领域列表）",
            )
        result = await self._activator.activate(domain)
        if not result.success:
            return ToolResult(success=False, error=result.message)
        return ToolResult(
            success=True,
            data={
                "domain": result.domain,
                "already_active": result.already_active,
                "new_tools": result.new_tools,
                "new_skills": result.new_skills,
                "message": result.message,
            },
        )
