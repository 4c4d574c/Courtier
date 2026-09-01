"""ListArtifactsTool — browse typed artifacts available in the current agent run."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from courtier.agent.artifacts.models import RuntimePolicy
from courtier.agent.artifacts.projectors import create_default_projector_registry
from courtier.prompts.errors import render_error

from ..protocol import OnToolProgress, ToolResult

if TYPE_CHECKING:
    from ..summary import ToolSummary


class ListArtifactsTool:
    """列出 artifact store 中当前可用的类型化数据工件。

    支持按类型、语义角色、主题过滤。返回工件元数据（类型、角色、来源、
    质量信息），用于在调用 get_artifact 之前了解有哪些数据可用。
    """

    name: str = "list_artifacts"
    display_name: str | None = "列出数据工件"
    skip_ref_resolution: bool = True
    skip_persist: bool = True
    skip_summarize: bool = True
    runtime_policy = RuntimePolicy(max_calls=5, max_consecutive=5)
    output_artifact_type: str | None = "core.debug_view"
    description: str = (
        "列出 artifact store 中当前可用的类型化数据工件。"
        "返回每个工件的 artifact_id、类型、语义角色、主题、来源工具、"
        "内容哈希和质量信息。"
        "需要浏览有哪些工件可用时调用本工具；"
        "若已知 $ref 引用（工具输出的 result_id），可直接调 get_artifact 获取数据。"
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "artifact_type": {
                "type": "string",
                "description": (
                    "按类型过滤，如 core.plain_text、docaudit.parsed_layout、"
                    "core.text_collection 等。不传则返回所有类型。"
                ),
            },
            "role": {
                "type": "string",
                "description": (
                    "按语义角色过滤："
                    "document（文档数据）、reference（参考/搜索结果数据）、"
                    "intermediate（中间数据）、debug（调试数据）。"
                ),
            },
            "subject": {
                "type": "string",
                "description": ("按主题过滤，如 current（当前文档）、search_results（搜索结果）。"),
            },
        },
    }

    async def execute(
        self,
        *,
        on_progress: OnToolProgress,
        artifact_store: Any = None,
        artifact_type: str | None = None,
        role: str | None = None,
        subject: str | None = None,
        **kwargs: Any,
    ) -> ToolResult:
        on_progress({"status": "running", "message": f"开始执行 {self.name}...", "detail": None})
        if artifact_store is None:
            on_progress({"status": "done", "message": "执行完成", "detail": None})
            return ToolResult(
                success=False,
                error=render_error("errors.artifact_store_unavailable", action="列出工件"),
            )

        candidates = artifact_store.list_projection_candidates()

        if artifact_type:
            candidates = [a for a in candidates if a.artifact_type == artifact_type]
        if role:
            candidates = [
                a
                for a in candidates
                if a.metadata.semantic_role == role or role in a.metadata.semantic_role
            ]
        if subject:
            candidates = [
                a
                for a in candidates
                if a.metadata.subject == subject or subject in a.metadata.subject
            ]

        # Build projection target map: source_type → [target_types]
        projector_registry = create_default_projector_registry()
        projection_targets: dict[str, list[str]] = {}
        for proj in projector_registry.all():
            src = proj.spec.source_type
            tgt = proj.spec.target_type
            if src not in projection_targets:
                projection_targets[src] = []
            if tgt not in projection_targets[src]:
                projection_targets[src].append(tgt)

        items = []
        for a in candidates:
            available_targets = projection_targets.get(a.artifact_type, [])
            items.append(
                {
                    "artifact_id": a.artifact_id,
                    "artifact_type": a.artifact_type,
                    "role": a.metadata.semantic_role,
                    "subject": a.metadata.subject,
                    "created_by": a.metadata.created_by,
                    "content_hash": a.metadata.content_hash,
                    "quality": a.metadata.quality,
                    "lineage": list(a.metadata.lineage),
                    "projectable_to_types": available_targets,
                }
            )

        on_progress({"status": "done", "message": "执行完成", "detail": None})
        return ToolResult(
            success=True,
            data={
                "artifacts": items,
                "count": len(items),
                "hint": (
                    "把上面某个 artifact_id 字段的值作为 get_artifact 工具的 "
                    "id 参数传入，即可获取该工件的数据（注意参数名是 id）；"
                    "需要把数据转换为另一种类型时，再附带 artifact_type 参数，"
                    "取值见该工件的 projectable_to_types 字段。"
                ),
            },
        )

    def summarize(self, result: ToolResult) -> "ToolSummary":
        from courtier.agent.tools.summary import summarize_result

        return summarize_result(result)
