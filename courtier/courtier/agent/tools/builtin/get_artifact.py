"""GetArtifactTool — retrieve persisted data or execute typed artifact projections."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from courtier.agent.artifacts.executor import (
    MaterializerRegistry,
    ProjectionExecutor,
    validate_materialized_value,
)
from courtier.agent.artifacts.models import (
    InputField,
    ProjectionPolicy,
    RuntimePolicy,
)
from courtier.agent.artifacts.projectors import create_default_projector_registry
from courtier.agent.artifacts.resolver import ProjectionResolver

from ..protocol import OnToolProgress, ToolResult

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ..summary import ToolSummary


class GetArtifactTool:
    """获取 artifact 数据 — 统一入口，行为由是否传 artifact_type 决定。

    两种使用方式：
    1. 只传 id：直接返回原始数据（从持久化存储读取或从 artifact 注册表查找）
    2. 传 id + artifact_type：执行类型投影链，将数据转换为目标类型
    """

    name: str = "get_artifact"
    display_name: str | None = "获取数据引用"
    skip_ref_resolution: bool = True
    skip_persist: bool = True
    skip_summarize: bool = True
    runtime_policy = RuntimePolicy(max_calls=30, max_consecutive=5)
    output_artifact_type: str | None = None
    description: str = (
        "获取 artifact 数据。统一使用 id 参数：\n"
        "1. 只传 id（$ref:tool:N 格式，来自工具输出中的 result_id 字段）："
        "直接返回原始数据。\n"
        "2. 传 id + artifact_type：执行类型投影，将数据转换为目标类型。\n"
        "支持 query/chunk_index/max_tokens 分页读取。"
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "id": {
                "type": "string",
                "description": (
                    "要获取的 artifact 标识。两种来源：\n"
                    "① $ref 引用（如 $ref:parse_document:1）—— 来自工具/Skill 输出中的"
                    " result_id 字段，最常用；\n"
                    "② artifact ID —— 来自 list_artifacts 的输出，"
                    "仅在需要类型投影时配合 artifact_type 使用。"
                ),
            },
            "artifact_type": {
                "type": "string",
                "description": (
                    "可选。目标 artifact 类型，用于执行类型投影链。\n"
                    "不传则直接返回原始数据（推荐，大多数情况不需要投影）。\n"
                    "常见类型：core.plain_text、core.text_collection、docaudit.paragraph_list 等。"
                ),
            },
            "query": {
                "type": "string",
                "description": "可选的查询字符串，用于只返回匹配片段。",
            },
            "chunk_index": {
                "type": "integer",
                "description": "分页读取时的 chunk 索引，从 0 开始。默认 0。",
                "default": 0,
            },
            "max_tokens": {
                "type": "integer",
                "description": "返回的最大 token 数（粗略按 4 字符/token 估算）。默认 2000。",
                "default": 2000,
            },
            "source_scope": {
                "type": "string",
                "enum": ["body", "header", "footer", "full_document"],
                "description": (
                    "限定提取的文档区域："
                    "body=正文、header=页眉、footer=页脚、full_document=全文。默认全文。"
                ),
            },
            "max_chars": {
                "type": "integer",
                "description": "最大返回字符数，超出部分截断。不传则不限制。",
            },
            "normalize_whitespace": {
                "type": "boolean",
                "description": "是否规范化空白字符（合并连续空格/换行）。默认 false。",
            },
            "max_items": {
                "type": "integer",
                "description": "最大返回条目数（对列表类型有效）。不传则不限制。",
            },
            "min_text_chars": {
                "type": "integer",
                "description": "最小文本字符数，低于此值的条目会被过滤。默认 0。",
            },
            "dedupe": {
                "type": "boolean",
                "description": "是否去除重复条目。默认 false。",
            },
            "materialize_as": {
                "type": "string",
                "description": (
                    "物化方式，如不指定则用类型的默认值。"
                    "常见值：string（纯文本字符串）、list_string（字符串列表）、dict。"
                ),
            },
            "label": {
                "type": "string",
                "description": "可选的语义标签，用于结果引用标记。",
            },
        },
    }

    async def execute(
        self,
        *,
        on_progress: OnToolProgress,
        artifact_store: Any = None,
        id: str = "",
        artifact_type: str = "",
        # Raw-data params
        query: str | None = None,
        chunk_index: int = 0,
        max_tokens: int = 2000,
        # Constraint params
        source_scope: str | None = None,
        max_chars: int | None = None,
        normalize_whitespace: bool | None = None,
        max_items: int | None = None,
        min_text_chars: int | None = None,
        dedupe: bool | None = None,
        materialize_as: str | None = None,
        label: str | None = None,
        **kwargs: Any,
    ) -> ToolResult:
        on_progress(
            {"status": "running", "message": f"开始执行 {self.name}...", "detail": None}
        )
        if artifact_store is None:
            on_progress({"status": "done", "message": "执行完成", "detail": None})
            return ToolResult(
                success=False,
                error="Artifact store 不可用，无法获取工件",
            )

        if not id:
            on_progress({"status": "done", "message": "执行完成", "detail": None})
            return ToolResult(
                success=False,
                error="必须提供 id 参数。"
                      "使用工具输出中的 result_id 字段（$ref:...:N 格式），"
                      "或 list_artifacts 返回的 artifact_id。",
            )

        # Unified dispatch: $ref → resolve from persistence, otherwise look up
        # in the typed artifact registry.
        if id.startswith("$ref:"):
            return await self._resolve_ref(
                artifact_store=artifact_store,
                id=id,
                artifact_type=artifact_type,
                query=query,
                chunk_index=chunk_index,
                max_tokens=max_tokens,
                source_scope=source_scope,
                max_chars=max_chars,
                normalize_whitespace=normalize_whitespace,
                max_items=max_items,
                min_text_chars=min_text_chars,
                dedupe=dedupe,
                materialize_as=materialize_as,
                label=label,
                on_progress=on_progress,
            )

        # Non-$ref id: must be a typed artifact in the registry.
        # Projection requires artifact_type.
        if not artifact_type:
            on_progress({"status": "done", "message": "执行完成", "detail": None})
            return ToolResult(
                success=False,
                error="使用非 $ref 的 artifact ID 时必须指定 artifact_type。"
                      "使用 list_artifacts 查看每个工件可用的 projectable_to_types。",
            )

        return await self._project_artifact(
            artifact_store=artifact_store,
            artifact_id=id,
            artifact_type=artifact_type,
            source_scope=source_scope,
            max_chars=max_chars,
            normalize_whitespace=normalize_whitespace,
            max_items=max_items,
            min_text_chars=min_text_chars,
            dedupe=dedupe,
            materialize_as=materialize_as,
            label=label,
            on_progress=on_progress,
        )

    async def _resolve_ref(
        self,
        *,
        artifact_store: Any,
        id: str,
        artifact_type: str,
        query: str | None,
        chunk_index: int,
        max_tokens: int,
        source_scope: str | None,
        max_chars: int | None,
        normalize_whitespace: bool | None,
        max_items: int | None,
        min_text_chars: int | None,
        dedupe: bool | None,
        materialize_as: str | None,
        label: str | None,
        on_progress: OnToolProgress,
    ) -> ToolResult:
        """Resolve a $ref id — read raw data, optionally project to target type."""

        try:
            raw = await artifact_store.read(
                id,
                query=query,
                chunk_index=chunk_index,
                max_tokens=max_tokens,
            )
        except Exception as exc:
            on_progress({"status": "done", "message": "执行完成", "detail": None})
            return ToolResult(
                success=False,
                error=f"读取持久化结果失败: {exc}",
            )

        if "error" in raw:
            on_progress({"status": "done", "message": "执行完成", "detail": None})
            return ToolResult(success=False, error=raw["error"])

        data = raw.get("data")
        if data is None:
            on_progress({"status": "done", "message": "执行完成", "detail": None})
            return ToolResult(
                success=False,
                error=f"结果 {id} 的数据为空",
            )

        # No artifact_type → return raw data directly (most common path).
        if not artifact_type:
            on_progress({"status": "done", "message": "执行完成", "detail": None})
            return ToolResult(
                success=True,
                data=data,
                metadata={
                    "result_id": id,
                    "query": query,
                    "chunk_index": chunk_index,
                    "max_tokens": max_tokens,
                    **raw.get("metadata", {}),
                },
            )

        # artifact_type specified → find/create a typed artifact, then project.
        source = self._find_artifact_for_ref(artifact_store, id)
        if source is None:
            source = artifact_store.register_cached_ref(
                ref_id=id,
                artifact_type="core.cached_output",
                created_by="get_artifact",
                data=data,
                role="intermediate",
                subject="unknown",
                projection_allowed=True,
                persist_to_disk=False,
            )

        return await self._project_artifact(
            artifact_store=artifact_store,
            artifact_id=source.artifact_id,
            artifact_type=artifact_type,
            source_scope=source_scope,
            max_chars=max_chars,
            normalize_whitespace=normalize_whitespace,
            max_items=max_items,
            min_text_chars=min_text_chars,
            dedupe=dedupe,
            materialize_as=materialize_as,
            label=label,
            on_progress=on_progress,
        )

    @staticmethod
    def _find_artifact_for_ref(artifact_store: Any, ref_id: str) -> Any | None:
        """Find an Artifact in the store whose artifact_id matches *ref_id*."""
        for artifact in artifact_store.list_all():
            if artifact.artifact_id == ref_id:
                return artifact
        return None

    async def _project_artifact(
        self,
        *,
        artifact_store: Any,
        artifact_id: str,
        artifact_type: str,
        source_scope: str | None,
        max_chars: int | None,
        normalize_whitespace: bool | None,
        max_items: int | None,
        min_text_chars: int | None,
        dedupe: bool | None,
        materialize_as: str | None,
        label: str | None,
        on_progress: OnToolProgress,
    ) -> ToolResult:
        """Execute type projection for *artifact_id* → *artifact_type*."""

        constraints: dict[str, Any] = {}
        if source_scope is not None:
            constraints["source_scope"] = source_scope
        if max_chars is not None:
            constraints["max_chars"] = max_chars
        if normalize_whitespace is not None:
            constraints["normalize_whitespace"] = normalize_whitespace
        if max_items is not None:
            constraints["max_items"] = max_items
        if min_text_chars is not None:
            constraints["min_text_chars"] = min_text_chars
        if dedupe is not None:
            constraints["dedupe"] = dedupe

        registry = create_default_projector_registry()
        resolver = ProjectionResolver(registry)
        policy = ProjectionPolicy()

        source = artifact_store.get(artifact_id)
        if source is None:
            on_progress({"status": "done", "message": "执行完成", "detail": None})
            if artifact_id.startswith("$ref:"):
                hint = (
                    f"（提示：{artifact_id} 是持久化引用，"
                    f"直接作为 id 传入即可读取原始数据，无需先调 list_artifacts。）"
                )
            else:
                hint = "请先调用 list_artifacts 查看可用工件及其 artifact_id。"
            return ToolResult(
                success=False,
                error=(
                    f"未找到 artifact_id={artifact_id} 的工件。{hint}"
                ),
            )

        field = InputField(
            name="value",
            artifact_type=artifact_type,
            materialize_as=materialize_as or _default_materialize_as(artifact_type),
            constraints=constraints,
        )
        fields = (field,)
        resolution = resolver.resolve(fields, "get_artifact", [source], policy)

        if resolution.status != "resolved":
            messages = [d.message for d in resolution.diagnostics]
            suggestions: list[dict[str, str]] = []
            for action in resolution.suggested_actions:
                suggestions.append(
                    {
                        "action": action.get("action", ""),
                        "tool": action.get("tool", ""),
                        "reason": action.get("reason", ""),
                    }
                )
            error_msg = "无法获取请求的 artifact：" + "; ".join(messages)
            if suggestions:
                error_msg += "。建议先调用以下上游工具：" + ", ".join(
                    s["tool"] for s in suggestions
                )
            on_progress({"status": "done", "message": "执行完成", "detail": None})
            return ToolResult(
                success=False,
                error=error_msg,
                metadata={"suggestions": suggestions},
            )

        validation_err = None
        try:
            executor = ProjectionExecutor(
                projector_registry=registry,
                materializer_registry=MaterializerRegistry.default(),
                artifact_store=artifact_store,
            )
            plan = resolution.plans["value"]
            binding = executor.execute(plan)

            field = fields[0]
            validation_err = validate_materialized_value(binding.value, field)
        except KeyError as exc:
            available_types = ", ".join(
                sorted(MaterializerRegistry.default().materializable_types)
            )
            on_progress({"status": "done", "message": "执行完成", "detail": None})
            return ToolResult(
                success=False,
                error=(
                    f"投影执行失败: {exc}。"
                    f"当前可物化的 artifact_type: {available_types}。"
                    f"如果源 artifact 类型（如 docaudit.parsed_document）不可直接物化，"
                    f"请尝试请求投影目标类型（如 core.plain_text 或 docaudit.paragraph_list），"
                    f"系统会自动执行类型投影链。"
                ),
            )
        except Exception as exc:
            logger.warning("Projection failed for artifact: %s", exc, exc_info=True)
            on_progress({"status": "done", "message": "执行完成", "detail": None})
            return ToolResult(
                success=False,
                error=f"投影执行失败: {exc}",
            )

        if validation_err is not None:
            on_progress({"status": "done", "message": "执行完成", "detail": None})
            return ToolResult(success=False, error=validation_err)

        source = artifact_store.get(plan.source_artifact_id)
        source_meta: dict[str, Any] = {
            "materializer": binding.materializer,
        }
        if source is not None:
            source_meta["content_hash"] = source.metadata.content_hash
            source_meta["quality"] = source.metadata.quality
            source_meta["lineage"] = list(source.metadata.lineage)

        result_metadata: dict[str, Any] = {}
        if label:
            result_metadata["label"] = label

        on_progress({"status": "done", "message": "执行完成", "detail": None})
        return ToolResult(
            success=True,
            data={
                "value": binding.value,
                "artifact_id": binding.artifact_id,
                "artifact_type": artifact_type,
                "trace": binding.trace.model_dump() if binding.trace else None,
                "metadata": source_meta,
            },
            metadata=result_metadata,
        )

    def summarize(self, result: ToolResult) -> "ToolSummary":
        from courtier.agent.tools.summary import summarize_result

        return summarize_result(result)


def _default_materialize_as(artifact_type: str) -> str:
    """Return the default materialize_as for known artifact types."""
    defaults: dict[str, str] = {
        "core.plain_text": "string",
        "core.text_collection": "list_string",
        "docaudit.document_metadata": "dict",
        "docaudit.paragraph_list": "dict",
        "docaudit.reference_text_list": "dict",
        "docaudit.search_results": "dict",
        "docaudit.parsed_document": "dict",
        "docaudit.audit_finding_list": "dict",
        "docaudit.audit_report": "dict",
        "docaudit.document_structure": "dict",
        "docaudit.plagiarism_report": "dict",
    }
    return defaults.get(artifact_type, "dict")
