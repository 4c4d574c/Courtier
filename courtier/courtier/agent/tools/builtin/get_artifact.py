"""GetArtifactTool — retrieve persisted data or execute typed artifact projections."""

from __future__ import annotations

import json
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
from courtier.agent.artifacts.projectors import (
    create_default_projector_registry,
    parsed_document_to_text,
)
from courtier.agent.artifacts.resolver import ProjectionResolver
from courtier.agent.core.cache_store import _TEXT_FIELD_PRIORITY
from courtier.prompts.errors import render_error

from ..protocol import OnToolProgress, ToolResult

logger = logging.getLogger(__name__)

# 投影目标候选：materialize_as → 目标类型优先级。"dict" 由
# _infer_projection_target 特判为源类型自身；候选逐个经 ProjectionResolver
# 按源类型可行性筛选，避免硬编码类型表。
_PROJECTION_TARGET_CANDIDATES: dict[str, tuple[str, ...]] = {
    "string": ("core.plain_text",),
    "list_string": ("docaudit.paragraph_list", "core.text_collection"),
    "list_dict": ("docaudit.paragraph_list", "docaudit.reference_text_list"),
}

if TYPE_CHECKING:
    from ..summary import ToolSummary


def _collect_constraints(
    *,
    source_scope: str | None,
    max_chars: int | None,
    normalize_whitespace: bool | None,
    max_items: int | None,
    min_text_chars: int | None,
    dedupe: bool | None,
) -> dict[str, Any]:
    """把投影约束参数收敛为 dict（None 剔除），_project_artifact 同源。"""
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
    return constraints


def _has_projection_request(
    *,
    materialize_as: str | None,
    source_scope: str | None,
    max_chars: int | None,
    normalize_whitespace: bool | None,
    max_items: int | None,
    min_text_chars: int | None,
    dedupe: bool | None,
) -> bool:
    """是否构成投影请求：任一物化/约束参数非空。"""
    return bool(
        materialize_as
        or source_scope is not None
        or max_chars is not None
        or normalize_whitespace is not None
        or max_items is not None
        or min_text_chars is not None
        or dedupe is not None
    )


def _infer_projection_target(
    source: Any,
    *,
    materialize_as: str | None,
    constraints: dict[str, Any],
) -> str | None:
    """为未显式给 artifact_type 的投影请求推断目标类型。

    依次尝试与 materialize_as 匹配的候选目标，取第一个能被
    ProjectionResolver 从源类型解析到的；仅约束参数（无 materialize_as）
    时只试纯文本目标——source_scope/max_chars 等约束只在文本投影链上生效。
    全部不可解析返回 None：调用方必须明确报错，禁止静默退回原始数据。
    """
    registry = create_default_projector_registry()
    resolver = ProjectionResolver(registry)
    policy = ProjectionPolicy()

    def _resolvable(target: str) -> bool:
        field = InputField(
            name="value",
            artifact_type=target,
            materialize_as=materialize_as or _default_materialize_as(target),
            constraints=constraints,
        )
        resolution = resolver.resolve((field,), "get_artifact", [source], policy)
        return resolution.status == "resolved"

    if materialize_as == "dict":
        return source.artifact_type if _resolvable(source.artifact_type) else None
    if materialize_as is None:
        candidates: tuple[str, ...] = ("core.plain_text",)
    else:
        candidates = _PROJECTION_TARGET_CANDIDATES.get(materialize_as, ())
    for target in candidates:
        if _resolvable(target):
            return target
    return None


def _looks_like_parsed_document(data: dict) -> bool:
    """探测 parsed_document 形状：pages 列表且首页含 page_content。"""
    pages = data.get("pages")
    if not isinstance(pages, list) or not pages:
        return False
    first = pages[0]
    return isinstance(first, dict) and isinstance(first.get("page_content"), dict)


def _has_semantic_constraints(
    *,
    source_scope: str | None,
    normalize_whitespace: bool | None,
    min_text_chars: int | None,
    dedupe: bool | None,
) -> bool:
    """语义类约束（改变数据内容/区域），与纯尺寸上限类约束区分。"""
    return (
        source_scope is not None
        or normalize_whitespace is not None
        or min_text_chars is not None
        or dedupe is not None
    )


def _raw_satisfies_caps(
    data: Any,
    *,
    max_chars: int | None,
    max_items: int | None,
) -> bool:
    """原始数据是否已满足尺寸上限类约束（无法投影时的回退判据）。

    max_chars/max_items 语义是"至多 N"：若原始数据本就不超上限，
    直接返回原始数据即满足请求，无需强制投影。
    """
    if max_chars is not None:
        size = len(data) if isinstance(data, str) else len(json.dumps(data, ensure_ascii=False))
        if size > max_chars:
            return False
    if max_items is not None:
        n = len(data) if isinstance(data, (list, dict)) else 1
        if n > max_items:
            return False
    return True


class GetArtifactTool:
    """获取 artifact 数据 — 统一入口，行为由是否传 artifact_type 决定。

    两种使用方式：
    1. 只传 id：直接返回原始数据（从持久化存储读取或从 artifact 注册表查找）
    2. 传 id + artifact_type：执行类型投影链，将数据转换为目标类型
    3. 只传 id + materialize_as/source_scope 等投影参数：按产物自身类型
       自动推断投影目标并执行投影（无需 artifact_type）；推断失败明确报错，
       不会静默返回原始数据。
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
                    '支持：序号（如 "3"、"三"、"第三节"）或标题文本（精确/包含匹配）。'
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
                    "对 $ref 产物生效时会自动投影为文本（无需同时传 artifact_type）。"
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
                    "对 $ref 产物生效时会按产物类型自动推断投影目标"
                    "（无需同时传 artifact_type）。"
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
                error=render_error("errors.artifact_store_unavailable", action="获取工件"),
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
                error=render_error("errors.artifact_missing_id"),
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
                    if not _has_projection_request(
                        materialize_as=materialize_as,
                        source_scope=source_scope,
                        max_chars=max_chars,
                        normalize_whitespace=normalize_whitespace,
                        max_items=max_items,
                        min_text_chars=min_text_chars,
                        dedupe=dedupe,
                    ):
                        return ToolResult(
                            success=True,
                            data=artifact.data,
                            metadata={"result_id": id, "artifact_id": id},
                        )
                    # 投影参数存在但未给目标类型：按产物自身类型推断目标。
                    # 推断失败必须明确报错——过去这里静默返回原始数据，
                    # 模型请求 string 物化却收到未物化的 dict。
                    constraints = _collect_constraints(
                        source_scope=source_scope,
                        max_chars=max_chars,
                        normalize_whitespace=normalize_whitespace,
                        max_items=max_items,
                        min_text_chars=min_text_chars,
                        dedupe=dedupe,
                    )
                    target = _infer_projection_target(
                        artifact,
                        materialize_as=materialize_as,
                        constraints=constraints,
                    )
                    if target is None:
                        if (
                            not materialize_as
                            and not _has_semantic_constraints(
                                source_scope=source_scope,
                                normalize_whitespace=normalize_whitespace,
                                min_text_chars=min_text_chars,
                                dedupe=dedupe,
                            )
                            and _raw_satisfies_caps(
                                artifact.data, max_chars=max_chars, max_items=max_items
                            )
                        ):
                            # 仅尺寸上限类约束且原始数据已满足上限："至多 N"的
                            # 语义成立，直接返回原始数据（无需强制投影）。
                            return ToolResult(
                                success=True,
                                data=artifact.data,
                                metadata={"result_id": id, "artifact_id": id},
                            )
                        return ToolResult(
                            success=False,
                            error=render_error(
                                "errors.artifact_projection_unsatisfied",
                                artifact_type=artifact.artifact_type,
                                materialize_as=materialize_as or "未指定",
                                constraints=sorted(constraints) or "无",
                            ),
                        )
                    return await self._project_artifact(
                        artifact_store=artifact_store,
                        artifact_id=id,
                        artifact_type=target,
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
                error=render_error("errors.artifact_data_not_found", id=id),
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
            if text is None and _looks_like_parsed_document(data):
                # parse_document 产物：文本嵌套在 pages[].page_content 里，
                # 走与投影器共用的提取路径（避免两处结构理解漂移）。
                text = parsed_document_to_text(data) or None

        on_progress({"status": "done", "message": "执行完成", "detail": None})
        if text is None:
            return ToolResult(
                success=False,
                error=render_error("errors.artifact_outline_unsupported", id=id),
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
                    error=render_error(
                        "errors.artifact_section_not_found",
                        section=section,
                        candidates=candidates,
                        truncated=len(sections) > 10,
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
        return ToolResult(
            success=False, error=render_error("errors.artifact_outline_missing_params")
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

        # 投影参数存在但未给目标类型：找带真实类型的工件做目标推断，
        # 推断失败明确报错（与 registry 分支同契约，禁止静默退回原始数据）。
        if not artifact_type and _has_projection_request(
            materialize_as=materialize_as,
            source_scope=source_scope,
            max_chars=max_chars,
            normalize_whitespace=normalize_whitespace,
            max_items=max_items,
            min_text_chars=min_text_chars,
            dedupe=dedupe,
        ):
            semantic = _has_semantic_constraints(
                source_scope=source_scope,
                normalize_whitespace=normalize_whitespace,
                min_text_chars=min_text_chars,
                dedupe=dedupe,
            )
            source = self._find_artifact_for_ref(artifact_store, id)
            constraints = _collect_constraints(
                source_scope=source_scope,
                max_chars=max_chars,
                normalize_whitespace=normalize_whitespace,
                max_items=max_items,
                min_text_chars=min_text_chars,
                dedupe=dedupe,
            )
            if source is not None:
                target = _infer_projection_target(
                    source,
                    materialize_as=materialize_as,
                    constraints=constraints,
                )
                if target is not None:
                    return await self._project_artifact(
                        artifact_store=artifact_store,
                        artifact_id=source.artifact_id,
                        artifact_type=target,
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
            # 投影不可行：仅尺寸上限类约束时，原始数据满足上限即语义成立，
            # 直接返回原始数据（"至多 N"不要求必须投影）。
            if not materialize_as and not semantic:
                raw = await artifact_store.read(
                    id,
                    query=None,
                    chunk_index=0,
                    max_tokens=max_tokens,
                )
                data = raw.get("data") if isinstance(raw, dict) and "error" not in raw else None
                if data is not None and _raw_satisfies_caps(
                    data, max_chars=max_chars, max_items=max_items
                ):
                    return ToolResult(
                        success=True,
                        data=data,
                        metadata={
                            "result_id": id,
                            **(raw.get("metadata", {}) if isinstance(raw, dict) else {}),
                        },
                    )
            if source is None:
                return ToolResult(
                    success=False,
                    error=render_error("errors.artifact_typed_ref_not_found", id=id),
                )
            return ToolResult(
                success=False,
                error=render_error(
                    "errors.artifact_projection_unsatisfied",
                    artifact_type=source.artifact_type,
                    materialize_as=materialize_as or "未指定",
                    constraints=sorted(constraints) or "无",
                ),
            )

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
                error=render_error("errors.artifact_read_failed", error=str(exc)),
            )

        if "error" in raw:
            on_progress({"status": "done", "message": "执行完成", "detail": None})
            error = str(raw["error"])
            if error.startswith("result not found"):
                error += "。" + render_error("errors.artifact_ref_not_found_hint")
            return ToolResult(success=False, error=error)

        data = raw.get("data")
        if data is None:
            on_progress({"status": "done", "message": "执行完成", "detail": None})
            return ToolResult(
                success=False,
                error=render_error("errors.artifact_empty_data", id=id),
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

        constraints = _collect_constraints(
            source_scope=source_scope,
            max_chars=max_chars,
            normalize_whitespace=normalize_whitespace,
            max_items=max_items,
            min_text_chars=min_text_chars,
            dedupe=dedupe,
        )

        registry = create_default_projector_registry()
        resolver = ProjectionResolver(registry)
        policy = ProjectionPolicy()

        source = artifact_store.get(artifact_id)
        if source is None:
            on_progress({"status": "done", "message": "执行完成", "detail": None})
            if artifact_id.startswith("$ref:"):
                hint = render_error("errors.artifact_hint_persisted_ref", artifact_id=artifact_id)
            else:
                hint = render_error("errors.artifact_hint_list_artifacts")
            return ToolResult(
                success=False,
                error=render_error(
                    "errors.artifact_id_not_found",
                    artifact_id=artifact_id,
                    hint=hint,
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
            error_msg = render_error(
                "errors.artifact_projection_resolve_failed",
                messages="; ".join(messages),
                suggested_tools=", ".join(s["tool"] for s in suggestions) if suggestions else "",
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
                error=render_error(
                    "errors.artifact_projection_exec_failed_typed",
                    error=str(exc),
                    available_types=available_types,
                ),
            )
        except Exception as exc:
            logger.warning("Projection failed for artifact: %s", exc, exc_info=True)
            on_progress({"status": "done", "message": "执行完成", "detail": None})
            return ToolResult(
                success=False,
                error=render_error("errors.artifact_projection_exec_failed", error=str(exc)),
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
