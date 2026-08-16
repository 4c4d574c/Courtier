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
from courtier.agent.artifacts.outline import find_section, parse_sections
from courtier.agent.artifacts.projectors import create_default_projector_registry
from courtier.agent.artifacts.resolver import ProjectionResolver
from courtier.agent.core.cache_store import _TEXT_FIELD_PRIORITY

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
        "获取 artifact 数据。id 为唯一必填参数：\n"
        "1. 只传 id —— 直接返回原始数据（最常用，覆盖绝大多数场景）。"
        "id 填工具/Skill 输出中的 result_id（$ref:...:N 格式），"
        "或 list_artifacts 输出中的 artifact_id 字段值。\n"
        "2. 传 id + artifact_type —— 仅在需要把数据【转换为另一种类型】时使用，"
        "系统执行类型投影链。\n"
        "支持 query/chunk_index/max_tokens 分页读取。\n"
        "长文档建议先传 outline=true 获取章节大纲，再用 section 参数按节定点读取。"
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "required": ["id"],
        "properties": {
            "id": {
                "type": "string",
                "description": (
                    "要获取的数据标识（本参数名为 id，不是 artifact_id）。两个来源：\n"
                    "① 工具/Skill 输出中的 result_id 字段（$ref:...:N 格式）—— 最常用；\n"
                    "② list_artifacts 输出中的 artifact_id 字段值 —— 原样填入本参数即可，"
                    "无需额外参数。"
                ),
            },
            "artifact_type": {
                "type": "string",
                "description": (
                    "可选。仅在需要【转换数据类型】时传，"
                    "取值见 list_artifacts 输出的 projectable_to_types"
                    "（如 core.plain_text、docaudit.paragraph_list）。\n"
                    "不传则按原始数据直接返回（推荐，大多数情况不需要转换）。"
                ),
            },
            "outline": {
                "type": "boolean",
                "description": (
                    "返回文档的结构化大纲（标题/层级/长度/位置）。"
                    "读取长文档时先传 outline=true 定位章节，再配合 section 参数定点读取，"
                    "默认 false。"
                ),
            },
            "section": {
                "type": "string",
                "description": (
                    "按大纲序号或标题文本读取指定节的完整内容（含子节）。"
                    "支持：序号（如 \"3\"、\"三\"、\"第三节\"）或标题文本（精确/包含匹配）。"
                    "超长节按 max_tokens 截断并标注。"
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
        outline: bool = False,
        section: str | None = None,
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
        on_progress({"status": "running", "message": f"开始执行 {self.name}...", "detail": None})
        if artifact_store is None:
            on_progress({"status": "done", "message": "执行完成", "detail": None})
            return ToolResult(
                success=False,
                error="Artifact store 不可用，无法获取工件",
            )

        # Tolerate the artifact_id alias: list_artifacts outputs an
        # ``artifact_id`` field, so models occasionally pass it under that
        # name instead of the canonical ``id`` parameter.
        if not id:
            alias = kwargs.get("artifact_id")
            if isinstance(alias, str) and alias:
                id = alias

        if not id:
            on_progress({"status": "done", "message": "执行完成", "detail": None})
            return ToolResult(
                success=False,
                error="必须提供 id 参数。"
                "使用工具输出中的 result_id 字段（$ref:...:N 格式），"
                "或 list_artifacts 返回的 artifact_id 字段值。",
            )

        # Structured reading (outline/section) bypasses the projection/paging
        # paths entirely — it needs the full text, not a truncated page.
        if outline or section:
            return await self._structured_read(
                artifact_store=artifact_store,
                id=id,
                outline=bool(outline),
                section=section,
                max_tokens=max_tokens,
                on_progress=on_progress,
            )

        # Registry-first dispatch for $ref ids: list_artifacts advertises
        # ids like ``$ref:<tool>:latest`` for small inline outputs — those
        # live only in the typed registry, and the persistence read path
        # below cannot see them (observed: model followed list_artifacts'
        # guidance and got "result not found").
        if id.startswith("$ref:"):
            artifact = artifact_store.get(id)
            if artifact is not None and getattr(artifact, "data", None) is not None:
                on_progress({"status": "done", "message": "执行完成", "detail": None})
                if not artifact_type:
                    return ToolResult(
                        success=True,
                        data=artifact.data,
                        metadata={"result_id": id, "artifact_id": id},
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
            # Registry miss — fall through to the persistence read path
            # (numbered refs from other processes/sessions resolve there).
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

        # Non-$ref id: a typed artifact in the registry. Without artifact_type
        # there is nothing to convert — return the stored data directly.
        if not artifact_type:
            artifact = artifact_store.get(id)
            data = getattr(artifact, "data", None) if artifact is not None else None
            on_progress({"status": "done", "message": "执行完成", "detail": None})
            if data is not None:
                return ToolResult(
                    success=True,
                    data=data,
                    metadata={"result_id": id, "artifact_id": id},
                )
            return ToolResult(
                success=False,
                error=(
                    f"未找到 id={id} 的工件数据。"
                    "id 参数可填：工具输出中的 result_id（$ref:...:N 格式），"
                    "或 list_artifacts 输出中的 artifact_id 字段值；"
                    "需要转换类型时再附带 artifact_type（取值见 projectable_to_types）。"
                ),
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

    async def _structured_read(
        self,
        *,
        artifact_store: Any,
        id: str,
        outline: bool,
        section: str | None,
        max_tokens: int,
        on_progress: OnToolProgress,
    ) -> ToolResult:
        """Outline/section structured reading over the FULL stored text.

        Registry first (same order as the main dispatch), then a full
        synchronous load (no token truncation — outline needs the whole
        text; the persistence read path truncates by max_tokens).
        """
        data: Any = None
        artifact = artifact_store.get(id)
        if artifact is not None:
            data = getattr(artifact, "data", None)
        if data is None:
            loader = getattr(artifact_store, "load", None)
            if callable(loader):
                try:
                    data = loader(id)
                except Exception:
                    logger.warning("structured read load failed for %s", id, exc_info=True)

        text: str | None = None
        if isinstance(data, str):
            text = data
        elif isinstance(data, dict):
            for field_name in _TEXT_FIELD_PRIORITY:
                value = data.get(field_name)
                if isinstance(value, str) and value.strip():
                    text = value
                    break

        on_progress({"status": "done", "message": "执行完成", "detail": None})
        if text is None:
            return ToolResult(
                success=False,
                error=(
                    f"结果 {id} 不含可解析的文本内容，无法生成大纲/按节读取。"
                ),
            )

        sections = parse_sections(text)
        if outline:
            return ToolResult(
                success=True,
                data={
                    "outline": [s.to_dict() for s in sections],
                    "total_chars": len(text),
                    "section_count": len(sections),
                },
                metadata={"result_id": id},
            )

        if section:
            found = find_section(sections, section)
            if found is None:
                candidates = "；".join(s.title for s in sections[:10])
                return ToolResult(
                    success=False,
                    error=(
                        f"未找到节「{section}」。可用节（序号+标题）: {candidates}"
                        + ("…" if len(sections) > 10 else "")
                    ),
                )
            from courtier.agent.core.loop_utils import truncate_data

            content = text[found.start_char : found.end_char]
            truncated_content = truncate_data(content, max_tokens)
            return ToolResult(
                success=True,
                data={
                    "section_index": found.index,
                    "section_title": found.title,
                    "section_chars": found.chars,
                    "content": truncated_content,
                    "truncated": len(truncated_content) < len(content),
                },
                metadata={"result_id": id},
            )
        return ToolResult(success=False, error="结构化读取需要 outline 或 section 参数")

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
            error = str(raw["error"])
            if error.startswith("result not found"):
                error += (
                    "。该引用不存在或不属于当前任务可见范围，"
                    "请使用当前任务链中工具实际返回的 result_id，"
                    "或先调用 list_artifacts 查看可用工件。"
                )
            return ToolResult(success=False, error=error)

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
        """Find an Artifact in the store whose artifact_id matches *ref_id*.

        When the exact id misses — e.g. the model derives a historical
        ``$ref:tool:N`` id while the store only keeps the newest
        ``$ref:tool:latest`` — fall back to the newest artifact of the same
        tool prefix so the reference still resolves with its real type
        (instead of degrading to an untyped cached_output that cannot be
        projected).
        """
        for artifact in artifact_store.list_all():
            if artifact.artifact_id == ref_id:
                return artifact
        if ref_id.startswith("$ref:") and ":" in ref_id:
            prefix = ref_id.rsplit(":", 1)[0] + ":"
            matches = [
                artifact
                for artifact in artifact_store.list_all()
                if artifact.artifact_id.startswith(prefix)
            ]
            if matches:
                # list_all preserves insertion order — the last match is the
                # most recent artifact produced by that tool.
                return matches[-1]
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
                error=(f"未找到 artifact_id={artifact_id} 的工件。{hint}"),
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
            available_types = ", ".join(sorted(MaterializerRegistry.default().materializable_types))
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
