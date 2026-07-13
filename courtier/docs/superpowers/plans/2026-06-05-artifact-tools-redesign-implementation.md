# Artifact 工具重构实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用 `list_artifacts` + `get_artifact` 替换 `read_cached_output`，让 LLM 可以直接与类型化 artifact/projector 系统交互。

**Architecture:** 新建两个工具类（ListArtifactsTool、GetArtifactTool），复用现有的 ProjectionResolver + ProjectionExecutor + MaterializerRegistry。删除 read_cached_output 并清理所有相关引用和特殊逻辑。

**Tech Stack:** Python, Pydantic, jq（仅限 get_artifact 不需要 jq — 整个 jq 依赖在 read_cached.py 删除后不再被工具层使用）

**设计规格:** `docs/superpowers/specs/2026-06-05-artifact-tools-redesign.md`

---

### Task 1: 创建 ListArtifactsTool

**Files:**
- Create: `src/agent/tools/builtin/list_artifacts.py`

- [ ] **Step 1: 编写 ListArtifactsTool 类**

```python
"""ListArtifactsTool — browse typed artifacts available in the current agent run."""

from __future__ import annotations

from typing import Any

from ..protocol import ToolResult
from src.agent.artifacts.models import (
    OutputArtifactContract,
    ToolOutputContract,
    ToolRuntimePolicy,
)


class ListArtifactsTool:
    """列出 artifact store 中可用的类型化数据工件。

    支持按类型、语义角色、主题过滤。返回工件元数据（类型、角色、来源、
    质量信息），用于在调用 get_artifact 之前了解有哪些数据可用。
    """

    name: str = "list_artifacts"
    skip_ref_resolution: bool = True
    runtime_policy = ToolRuntimePolicy(
        hidden_from_task_agents_by_default=False,
    )
    output_contract = ToolOutputContract(
        tool_name="list_artifacts",
        outputs={
            "result": OutputArtifactContract(
                artifact_type="core.debug_view",
                role="debug",
                subject="debug",
                projection_allowed=False,
                debug_only=True,
            )
        },
    )
    description: str = (
        "列出 artifact store 中当前可用的类型化数据工件。"
        "返回每个工件的 artifact_id、类型、语义角色、主题、来源工具、"
        "内容哈希和质量信息。"
        "用于在调用 get_artifact 之前了解有哪些数据可用，"
        "以及它们能投影到哪些目标类型。"
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "artifact_type": {
                "type": "string",
                "description": (
                    "按类型过滤，如 core.plain_text、docaudit.parsed_document、"
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
                "description": (
                    "按主题过滤，如 current（当前文档）、search_results（搜索结果）。"
                ),
            },
        },
    }

    async def execute(
        self,
        artifact_store: Any = None,
        artifact_type: str | None = None,
        role: str | None = None,
        subject: str | None = None,
        **kwargs: Any,
    ) -> ToolResult:
        if artifact_store is None:
            return ToolResult(
                success=False,
                error="Artifact store 不可用，无法列出工件",
            )

        candidates = artifact_store.list_projection_candidates()

        if artifact_type:
            candidates = [
                a for a in candidates if a.artifact_type == artifact_type
            ]
        if role:
            candidates = [
                a for a in candidates if a.metadata.semantic_role == role
            ]
        if subject:
            candidates = [
                a for a in candidates if a.metadata.subject == subject
            ]

        items = []
        for a in candidates:
            items.append({
                "artifact_id": a.artifact_id,
                "artifact_type": a.artifact_type,
                "role": a.metadata.semantic_role,
                "subject": a.metadata.subject,
                "created_by": a.metadata.created_by,
                "content_hash": a.metadata.content_hash,
                "quality": a.metadata.quality,
                "lineage": list(a.metadata.lineage),
            })

        return ToolResult(
            success=True,
            data={
                "artifacts": items,
                "count": len(items),
                "hint": "使用 get_artifact 按需获取投影后的数据",
            },
        )
```

- [ ] **Step 2: Commit**

```bash
git add src/agent/tools/builtin/list_artifacts.py
git commit -m "feat: add list_artifacts tool for browsing typed artifact store"
```

---

### Task 2: 创建 GetArtifactTool

**Files:**
- Create: `src/agent/tools/builtin/get_artifact.py`

- [ ] **Step 1: 编写 GetArtifactTool 类**

```python
"""GetArtifactTool — request typed artifact projections on demand."""

from __future__ import annotations

from typing import Any

from ..protocol import ToolResult
from src.agent.artifacts.executor import (
    MaterializerRegistry,
    ProjectionExecutor,
    validate_materialized_value,
)
from src.agent.artifacts.models import (
    ContractField,
    OutputArtifactContract,
    ProjectionPolicy,
    ToolInputContract,
    ToolOutputContract,
    ToolRuntimePolicy,
    upstream_producer_for,
)
from src.agent.artifacts.projectors import create_default_projector_registry
from src.agent.artifacts.resolver import ProjectionResolver


class GetArtifactTool:
    """从 artifact store 获取指定类型的数据，自动执行所需的类型投影。

    可以直接指定 artifact_id 精确获取，或让系统根据类型和约束自动选择
    最佳来源并执行投影链。
    """

    name: str = "get_artifact"
    skip_ref_resolution: bool = True
    runtime_policy = ToolRuntimePolicy(
        max_calls_per_agent_run=30,
        max_consecutive_calls=5,
        allow_chaining=False,
        hidden_from_task_agents_by_default=False,
    )
    output_contract = ToolOutputContract(
        tool_name="get_artifact",
        outputs={
            "result": OutputArtifactContract(
                artifact_type="core.debug_view",
                role="debug",
                subject="debug",
                projection_allowed=False,
                debug_only=True,
            )
        },
    )
    description: str = (
        "从 artifact store 获取指定类型的数据，自动执行所需的类型投影。"
        "支持两种模式：\n"
        "1. 自动选择：只提供 artifact_type（和可选的 role/subject/constraints），"
        "系统自动找到最佳匹配的 artifact 并执行所需的投影链。\n"
        "2. 精确指定：提供 artifact_id，直接对指定 artifact 执行投影。\n"
        "返回物化后的具体值、artifact 来源和投影追溯。"
        "当找不到匹配的 artifact 时，返回建议的上游工具。"
        "典型用法：先调用 list_artifacts 了解可用数据，"
        "再调用 get_artifact 按需获取投影后的数据。"
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "artifact_type": {
                "type": "string",
                "description": (
                    "目标 artifact 类型，如 core.plain_text、core.text_collection、"
                    "docaudit.paragraph_list、docaudit.reference_text_list 等。"
                ),
            },
            "artifact_id": {
                "type": "string",
                "description": (
                    "指定具体的 artifact ID（从 list_artifacts 获取）。"
                    "如果指定，系统直接对该 artifact 执行投影到目标类型。"
                    "如果不指定，系统自动选择最佳匹配的 artifact。"
                ),
            },
            "role": {
                "type": "string",
                "description": (
                    "指定语义角色（自动选择模式时使用）："
                    "document / reference / intermediate。"
                ),
            },
            "subject": {
                "type": "string",
                "description": (
                    "指定主题（自动选择模式时使用）："
                    "current / search_results。"
                ),
            },
            "constraints": {
                "type": "object",
                "description": (
                    "传递给 projector 的约束参数：\n"
                    "- source_scope: body / header / footer / full_document\n"
                    "- max_chars: 最大字符数截断\n"
                    "- normalize_whitespace: true/false\n"
                    "- max_items: 最大条目数\n"
                    "- min_text_chars: 最小文本字符数\n"
                    "- dedupe: true/false 去重\n"
                    "例如：{\"source_scope\": \"body\", \"max_chars\": 5000}"
                ),
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
        "required": ["artifact_type"],
    }

    async def execute(
        self,
        artifact_store: Any = None,
        artifact_type: str = "",
        artifact_id: str | None = None,
        role: str | None = None,
        subject: str | None = None,
        constraints: dict[str, Any] | None = None,
        materialize_as: str | None = None,
        label: str | None = None,
        **kwargs: Any,
    ) -> ToolResult:
        if artifact_store is None:
            return ToolResult(
                success=False,
                error="Artifact store 不可用，无法获取工件",
            )

        registry = create_default_projector_registry()
        resolver = ProjectionResolver(registry)
        policy = ProjectionPolicy()

        if artifact_id:
            # -- Exact artifact mode --
            source = artifact_store.get(artifact_id)
            if source is None:
                return ToolResult(
                    success=False,
                    error=f"未找到 artifact_id={artifact_id} 的工件。"
                           f"使用 list_artifacts 查看可用工件。",
                )
            # Use source's own role/subject if not explicitly provided
            effective_role = role or source.metadata.semantic_role
            effective_subject = subject or source.metadata.subject

            # Build a single-field contract for resolution
            field = ContractField(
                name="value",
                artifact_type=artifact_type,
                role=effective_role,
                subject=effective_subject,
                materialize_as=materialize_as or _default_materialize_as(artifact_type),
                constraints=constraints or {},
            )
            contract = ToolInputContract(
                tool_name="get_artifact",
                fields=(field,),
            )
            resolution = resolver.resolve(
                contract,
                [source],
                policy,
            )
        else:
            # -- Auto-select mode --
            candidates = artifact_store.list_projection_candidates()
            if not candidates:
                return ToolResult(
                    success=False,
                    error="Artifact store 中没有可用的工件。"
                           "请先调用上游工具（如 parse_document 或 search_documents）生成数据。",
                )

            field = ContractField(
                name="value",
                artifact_type=artifact_type,
                role=role or "intermediate",
                subject=subject or "unknown",
                materialize_as=materialize_as or _default_materialize_as(artifact_type),
                constraints=constraints or {},
            )
            contract = ToolInputContract(
                tool_name="get_artifact",
                fields=(field,),
            )
            resolution = resolver.resolve(
                contract,
                candidates,
                policy,
            )

        if resolution.status != "resolved":
            # Build error message with suggestions
            messages = [d.message for d in resolution.diagnostics]
            suggestions: list[dict[str, str]] = []
            for action in resolution.suggested_actions:
                suggestions.append({
                    "action": action.get("action", ""),
                    "tool": action.get("tool", ""),
                    "reason": action.get("reason", ""),
                })
            if not suggestions:
                producer = upstream_producer_for(artifact_type)
                if producer:
                    suggestions.append({
                        "action": "call_tool",
                        "tool": producer,
                        "reason": f"{producer} 输出 {artifact_type} 类型的数据",
                    })
            error_msg = "无法获取请求的 artifact：" + "; ".join(messages)
            if suggestions:
                error_msg += "。建议先调用以下上游工具：" + ", ".join(
                    s["tool"] for s in suggestions
                )
            return ToolResult(
                success=False,
                error=error_msg,
                metadata={"suggestions": suggestions},
            )

        # Execute the best projection plan
        executor = ProjectionExecutor(
            projector_registry=registry,
            materializer_registry=MaterializerRegistry.default(),
            artifact_store=artifact_store,
        )
        plan = resolution.plans["value"]
        binding = executor.execute(plan)

        # Validate materialized value
        field = contract.fields[0]
        validation_err = validate_materialized_value(binding.value, field)
        if validation_err is not None:
            return ToolResult(
                success=False,
                error=validation_err,
            )

        metadata: dict[str, Any] = {}
        if label:
            metadata["label"] = label

        return ToolResult(
            success=True,
            data={
                "value": binding.value,
                "artifact_id": binding.artifact_id,
                "artifact_type": artifact_type,
                "trace": binding.trace.model_dump() if binding.trace else None,
                "metadata": {
                    "materializer": binding.materializer,
                },
            },
            metadata=metadata,
        )


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
```

- [ ] **Step 2: Commit**

```bash
git add src/agent/tools/builtin/get_artifact.py
git commit -m "feat: add get_artifact tool for on-demand typed artifact projection"
```

---

### Task 3: 更新 ToolRegistry — 验证 cleanup 完成 ✅

**Files:**
- Modify: `src/agent/tools/registry.py`

Registry 已完成以下变更（无需额外操作）：

- [x] **已移除 `_READ_CACHED_OUTPUT_REF_RE` 正则** — 不再需要
- [x] **`get_schemas` 使用 `hide_debug_tools_for_task_agents`** — 已泛化，不再硬编码 read_cached_output
- [x] **`_check_runtime_policy` 无 read_cached_output 特殊逻辑** — 仅使用泛型 `ToolRuntimePolicy` 字段（`max_calls_per_agent_run`、`max_consecutive_calls`）。`get_artifact` 通过其自身的 `runtime_policy` 强制执行限制，无需特殊链式检测
- [x] **`import re` 已移除** — 不再需要用于 ref 模式匹配
- [x] **注释已更新** — 不再引用 read_cached_output

验证清单：
1. 确认 `hide_debug_tools_for_task_agents` feature flag 在 ProjectionFeatureFlags 中存在 ✅ (models.py:517)
2. 确认 `_check_runtime_policy` 仅使用泛型检查 ✅ (registry.py:221-272)
3. 确认 `get_schemas` 引用 `hide_debug_tools_for_task_agents` ✅ (registry.py:89)

---

### Task 4: 更新 AuditorAgent — 替换工具注入

**Files:**
- Modify: `src/agent/agents/base.py`

- [ ] **Step 1: 替换导入和注入**

将第 285 行和第 294 行：
```python
from ..tools.builtin.read_cached import ReadCachedOutputTool
...
tools.append(ReadCachedOutputTool())
```
替换为：
```python
from ..tools.builtin.list_artifacts import ListArtifactsTool
from ..tools.builtin.get_artifact import GetArtifactTool
...
tools.append(ListArtifactsTool())
tools.append(GetArtifactTool())
```

- [ ] **Step 2: Commit**

```bash
git add src/agent/agents/base.py
git commit -m "refactor: replace ReadCachedOutputTool with ListArtifactsTool + GetArtifactTool in AuditorAgent"
```

---

### Task 5: 更新 TransformAgent — 替换工具引用

**Files:**
- Modify: `src/agent/agents/transform.py`

- [ ] **Step 1: 替换导入和工具列表**

将第 13 行：
```python
from ..tools.builtin.read_cached import ReadCachedOutputTool
```
替换为：
```python
from ..tools.builtin.list_artifacts import ListArtifactsTool
from ..tools.builtin.get_artifact import GetArtifactTool
```

将第 29 行 role 字符串中的 `read_cached_output` 替换为 `get_artifact`：
```python
"1. 首先调用 get_artifact 逐个获取任务中列出的工件数据；\n"
```

将第 37 行：
```python
tools=[ReadCachedOutputTool(), PersistOutputTool()],
```
替换为：
```python
tools=[ListArtifactsTool(), GetArtifactTool(), PersistOutputTool()],
```

- [ ] **Step 2: Commit**

```bash
git add src/agent/agents/transform.py
git commit -m "refactor: replace ReadCachedOutputTool with new artifact tools in TransformAgent"
```

---

### Task 6: 更新 OrchestratorAgent — 替换工具和 prompt

**Files:**
- Modify: `src/agent/agents/orch.py`

- [ ] **Step 1: 替换导入**

将第 157-158 行：
```python
from ..tools.builtin.read_cached import ReadCachedOutputTool
from ..tools.builtin.persist_output import PersistOutputTool
```
替换为：
```python
from ..tools.builtin.list_artifacts import ListArtifactsTool
from ..tools.builtin.get_artifact import GetArtifactTool
from ..tools.builtin.persist_output import PersistOutputTool
```

- [ ] **Step 2: 更新注释和工具列表**

将第 160-165 行：
```python
# Build tools from subagents, plus parent-level cache tools.
# read_cached_output and persist_output are available so the
# orchestrator can inspect persisted sub-agent results and
# persist its own intermediate data when needed.
tools = self._subagent_runner.build_tools()
tools.append(ReadCachedOutputTool())
tools.append(PersistOutputTool())
```
替换为：
```python
# Build tools from subagents, plus parent-level artifact tools.
# list_artifacts, get_artifact, and persist_output are available so the
# orchestrator can inspect artifact store data and persist
# its own intermediate data when needed.
tools = self._subagent_runner.build_tools()
tools.append(ListArtifactsTool())
tools.append(GetArtifactTool())
tools.append(PersistOutputTool())
```

- [ ] **Step 3: 更新 role prompt**

将第 182-183 行：
```python
"4. 数据探索使用 data_shape._field_paths 查看可用字段路径，"
"非必要时不要调用 read_cached_output。"
```
替换为：
```python
"4. 使用 list_artifacts 查看可用的类型化工件及其元数据，"
"使用 get_artifact 按需获取投影后的数据。非必要时不要调用 get_artifact。"
```

- [ ] **Step 4: Commit**

```bash
git add src/agent/agents/orch.py
git commit -m "refactor: replace ReadCachedOutputTool with new artifact tools in OrchestratorAgent"
```

---

### Task 7: 更新 loop.py — 探索性工具追踪和提示消息

**Files:**
- Modify: `src/agent/core/loop.py`

- [ ] **Step 1: 更新 _EXPLORATORY_TOOLS**

将第 43 行：
```python
_EXPLORATORY_TOOLS = frozenset({"read_cached_output"})
```
替换为：
```python
_EXPLORATORY_TOOLS = frozenset({"get_artifact", "list_artifacts"})
```

- [ ] **Step 2: 删除 read_cached_output 链式调用守卫函数**

删除第 49-61 行（`_READ_CACHED_REF_RE_PRE` 和 `_is_read_cached_chain_call` 函数）。

- [ ] **Step 3: 删除 agent_loop 中的链式调用守卫**

删除第 331-359 行（`if _is_read_cached_chain_call(tool_call):` 整个块）。

- [ ] **Step 4: 更新注释**

将第 35-37 行注释：
```python
# Max consecutive read_cached_output / exploratory calls without a substantive
# tool call.  Prevents data-exploration loops where the agent extracts one field
# per call without ever reaching the actual audit/detection step.
```
替换为：
```python
# Max consecutive get_artifact / list_artifacts exploratory calls without a
# substantive tool call.  Prevents data-exploration loops where the agent
# fetches artifacts endlessly without calling a business tool.
```

将第 128 行注释：
```python
# Hide debug tools (read_cached_output) from normal task agents
```
替换为：
```python
# Hide debug tools from normal task agents
```

- [ ] **Step 5: 更新终端工具就绪提示消息**

将第 445-448 行：
```python
hint_msg = (
    "\n\n[系统提示] 以下业务工具所需参数已自动准备就绪，"
    "可直接调用，无需再通过 read_cached_output 探索缓存：\n"
    + tool_hints
)
```
替换为：
```python
hint_msg = (
    "\n\n[系统提示] 以下业务工具所需参数已自动准备就绪，"
    "可直接调用，无需再通过 get_artifact 获取数据：\n"
    + tool_hints
)
```

将第 463-466 行：
```python
hint_msg = (
    "\n\n[系统提示] 业务工具（如 detect_plagiarism）所需的参数"
    "已自动准备就绪。请直接调用目标业务工具，"
    "无需再调用 read_cached_output 探索缓存。"
)
```
替换为：
```python
hint_msg = (
    "\n\n[系统提示] 业务工具（如 detect_plagiarism）所需的参数"
    "已自动准备就绪。请直接调用目标业务工具，"
    "无需再调用 get_artifact 或 list_artifacts。"
)
```

- [ ] **Step 6: 更新 blocked_tools_hints 消息**

将第 497-499 行：
```python
+ "\n请先调用建议的上游工具获取所需数据，不要通过 read_cached_output 尝试构造参数。"
```
替换为：
```python
+ "\n请先调用建议的上游工具获取所需数据。"
```

- [ ] **Step 7: 更新 _check_explore_loop 注释**

将第 741-742 行注释：
```python
    3. Too many consecutive read-only/exploratory tool calls (e.g.
       read_cached_output) without a substantive tool call.
```
替换为：
```python
    3. Too many consecutive read-only/exploratory tool calls (e.g.
       get_artifact, list_artifacts) without a substantive tool call.
```

- [ ] **Step 8: 更新 _update_exploratory_tracking 注释**

将第 848-849 行：
```python
    read_cached_output.
```
替换为：
```python
    get_artifact.
```

- [ ] **Step 9: Commit**

```bash
git add src/agent/core/loop.py
git commit -m "refactor: update loop guards from read_cached_output to get_artifact"
```

---

### Task 8: 更新 context_manager.py — ref 指令和微压缩

**Files:**
- Modify: `src/agent/core/context_manager.py`

- [ ] **Step 1: 更新注释**

将第 78 行：
```python
# cache_dir so read_cached_output can also find files.
```
替换为：
```python
# cache_dir so legacy debug tools can also find files.
```

- [ ] **Step 2: 更新 micro_compact 方法**

将第 243 行注释：
```python
first so it can be recovered via read_cached_output.
```
替换为：
```python
first so it can be recovered if needed.
```

将第 262-275 行整个 read_cached_output 特殊处理块删除，改为统一逻辑：

删除：
```python
                # Skip read_cached_output: these are debug-only lookups;
                # creating $ref:read_cached_output:N placeholders invites
                # unnecessary re-reads downstream.
                ref_id: str | None = None
                if tool_name != "read_cached_output":
                    ref_id = self._extract_ref_id(msg)
                    if ref_id is None:
                        ref_id = self._persist_tool_message(msg, tool_name)

                note = (
                    "Result omitted by micro-compact (old tool result)."
                    if tool_name == "read_cached_output"
                    else "Result omitted by micro-compact (old tool result). "
                         "Use read_cached_output with ref_id to recover full data."
                )
```

替换为：
```python
                ref_id = self._extract_ref_id(msg)
                if ref_id is None:
                    ref_id = self._persist_tool_message(msg, tool_name)

                note = (
                    "Result omitted by micro-compact (old tool result). "
                    "Use $ref id as tool argument to recover full data."
                )
```

- [ ] **Step 3: 更新 get_ref_instructions 方法**

将整个方法体（第 459-468 行）替换为：
```python
    def get_ref_instructions(self) -> str:
        """Return a short instruction block for the LLM about how to use ref IDs."""
        return (
            "工具结果可能保存为 $ref 缓存引用。构造下游工具参数时，"
            "优先直接把业务 $ref 传给目标工具，系统会自动加载或适配参数。\n"
            "使用 list_artifacts 查看可用的类型化工件及其元数据，"
            "使用 get_artifact 按需获取投影后的数据。\n"
            "当工具返回 __persisted_output__ 标记时，预览通常已足够理解结果。"
            "旧工具结果可能被微压缩为 _omitted 占位符。"
            "普通业务流程不应依赖恢复这些调试内容。"
        )
```

- [ ] **Step 4: Commit**

```bash
git add src/agent/core/context_manager.py
git commit -m "refactor: remove read_cached_output references from context_manager"
```

---

### Task 9: 更新 models.py — 重命名 feature flag

**Files:**
- Modify: `src/agent/artifacts/models.py`

- [ ] **Step 1: 重命名 feature flag**

将第 517 行：
```python
hide_read_cached_output_for_task_agents: bool = True
```
替换为：
```python
hide_debug_tools_for_task_agents: bool = True
```

将第 602 行注释中的 `read_cached_output` 引用：
```python
debug_read: bool = False  # allow read_cached_output on this artifact
```

替换为：
```python
debug_read: bool = False  # allow debug_read access on this artifact
```

- [ ] **Step 2: 检查其他引用并更新**

`grep -rn "hide_read_cached_output" --include="*.py"` 查找所有引用。

`src/agent/tools/registry.py:95` 已经在 Task 3 中更新。

- [ ] **Step 3: Commit**

```bash
git add src/agent/artifacts/models.py
git commit -m "refactor: rename hide_read_cached_output_for_task_agents to hide_debug_tools_for_task_agents"
```

---

### Task 10: 删除 read_cached.py 和更新 subagent.py

**Files:**
- Delete: `src/agent/tools/builtin/read_cached.py`
- Modify: `src/agent/agents/subagent.py`

- [ ] **Step 1: 更新 subagent.py 中的引用**

将第 648 行：
```python
f"以下缓存数据的引用 ID 可用（无需调用 read_cached_output 加载）：\n"
```
替换为：
```python
f"以下缓存数据的引用 ID 可用（直接作为工具参数传入即可）：\n"
```

- [ ] **Step 2: 删除 read_cached.py**

```bash
git rm src/agent/tools/builtin/read_cached.py
```

- [ ] **Step 3: Commit**

```bash
git add src/agent/agents/subagent.py
git commit -m "refactor: delete read_cached_output tool, update subagent ref instructions"
```

---

### Task 11: 编写 list_artifacts 测试

**Files:**
- Create: `tests/agent/tools/test_list_artifacts.py`

- [ ] **Step 1: 编写测试**

```python
"""Tests for ListArtifactsTool."""

import asyncio

from src.agent.tools.builtin.list_artifacts import ListArtifactsTool
from src.agent.artifacts.store import ArtifactStore
from src.agent.artifacts.models import ArtifactMetadata, Artifact


def _make(
    aid: str,
    atype: str = "core.plain_text",
    role: str = "document",
    subject: str = "current",
) -> Artifact:
    return Artifact(
        artifact_id=aid,
        artifact_type=atype,
        data={"text": "test"},
        metadata=ArtifactMetadata(
            created_by="parse_document",
            semantic_role=role,
            subject=subject,
            content_hash="sha256:abc",
        ),
    )


class TestListArtifactsTool:
    def test_empty_store(self):
        store = ArtifactStore()
        result = asyncio.run(ListArtifactsTool().execute(artifact_store=store))
        assert result.success
        assert result.data["artifacts"] == []
        assert result.data["count"] == 0

    def test_all_candidates(self):
        store = ArtifactStore()
        store.put(_make("a1"))
        store.put(_make("a2", "docaudit.paragraph_list"))
        result = asyncio.run(ListArtifactsTool().execute(artifact_store=store))
        assert result.success
        assert result.data["count"] == 2

    def test_filter_by_type(self):
        store = ArtifactStore()
        store.put(_make("a1"))
        store.put(_make("a2", "docaudit.paragraph_list"))
        result = asyncio.run(
            ListArtifactsTool().execute(artifact_store=store, artifact_type="core.plain_text")
        )
        assert result.success
        assert result.data["count"] == 1
        assert result.data["artifacts"][0]["artifact_type"] == "core.plain_text"

    def test_filter_by_role(self):
        store = ArtifactStore()
        store.put(_make("a1", role="document"))
        store.put(_make("a2", role="reference"))
        result = asyncio.run(
            ListArtifactsTool().execute(artifact_store=store, role="reference")
        )
        assert result.success
        assert result.data["count"] == 1
        assert result.data["artifacts"][0]["role"] == "reference"

    def test_filter_by_subject(self):
        store = ArtifactStore()
        store.put(_make("a1", subject="current"))
        store.put(_make("a2", subject="search_results"))
        result = asyncio.run(
            ListArtifactsTool().execute(artifact_store=store, subject="search_results")
        )
        assert result.success
        assert result.data["count"] == 1
        assert result.data["artifacts"][0]["subject"] == "search_results"

    def test_excludes_debug_only(self):
        store = ArtifactStore()
        store.put(_make("a1"))
        debug = Artifact(
            artifact_id="a2",
            artifact_type="core.debug_view",
            data={"text": "debug"},
            metadata=ArtifactMetadata(
                created_by="debug_tool",
                semantic_role="debug",
                subject="debug",
                debug_only=True,
                projection_allowed=False,
            ),
        )
        store.put(debug)
        result = asyncio.run(ListArtifactsTool().execute(artifact_store=store))
        assert result.success
        assert result.data["count"] == 1
        assert result.data["artifacts"][0]["artifact_type"] == "core.plain_text"

    def test_store_none(self):
        result = asyncio.run(ListArtifactsTool().execute(artifact_store=None))
        assert not result.success
        assert "不可用" in result.error

    def test_combined_filters(self):
        store = ArtifactStore()
        store.put(_make("a1", "core.plain_text", "document", "current"))
        store.put(_make("a2", "core.plain_text", "reference", "search_results"))
        store.put(_make("a3", "core.text_collection", "document", "current"))
        result = asyncio.run(
            ListArtifactsTool().execute(
                artifact_store=store,
                artifact_type="core.plain_text",
                role="document",
                subject="current",
            )
        )
        assert result.success
        assert result.data["count"] == 1
        assert result.data["artifacts"][0]["artifact_id"] == "a1"
```

- [ ] **Step 2: 运行测试验证通过**

```bash
PYTHONPATH=. uv run python -m pytest tests/agent/tools/test_list_artifacts.py -v
```

- [ ] **Step 3: Commit**

```bash
git add tests/agent/tools/test_list_artifacts.py
git commit -m "test: add tests for list_artifacts tool"
```

---

### Task 12: 编写 get_artifact 测试

**Files:**
- Create: `tests/agent/tools/test_get_artifact.py`

- [ ] **Step 1: 编写测试**

```python
"""Tests for GetArtifactTool."""

import asyncio

from src.agent.tools.builtin.get_artifact import GetArtifactTool
from src.agent.artifacts.store import ArtifactStore
from src.agent.artifacts.models import ArtifactMetadata, Artifact


def _make(
    aid: str,
    atype: str = "core.plain_text",
    data: dict | None = None,
    role: str = "document",
    subject: str = "current",
) -> Artifact:
    return Artifact(
        artifact_id=aid,
        artifact_type=atype,
        data=data or {"text": "hello", "language": "zh", "source_scope": "full_document"},
        metadata=ArtifactMetadata(
            created_by="test",
            semantic_role=role,
            subject=subject,
            content_hash="sha256:x",
        ),
    )


class TestGetArtifactTool:
    def test_direct_match(self):
        store = ArtifactStore()
        store.put(_make("a1"))
        result = asyncio.run(
            GetArtifactTool().execute(
                artifact_store=store,
                artifact_type="core.plain_text",
                role="document",
                subject="current",
            )
        )
        assert result.success
        assert result.data["value"] == "hello"

    def test_exact_id(self):
        store = ArtifactStore()
        store.put(_make("a1"))
        store.put(_make("a2", data={"text": "world", "language": "zh", "source_scope": "full_document"}))
        result = asyncio.run(
            GetArtifactTool().execute(
                artifact_store=store,
                artifact_type="core.plain_text",
                artifact_id="a2",
            )
        )
        assert result.success
        assert result.data["value"] == "world"

    def test_no_match(self):
        store = ArtifactStore()
        store.put(_make("a1"))
        result = asyncio.run(
            GetArtifactTool().execute(
                artifact_store=store,
                artifact_type="docaudit.audit_finding_list",
            )
        )
        assert not result.success
        assert "suggestions" in result.metadata

    def test_id_not_found(self):
        result = asyncio.run(
            GetArtifactTool().execute(
                artifact_store=ArtifactStore(),
                artifact_type="core.plain_text",
                artifact_id="x",
            )
        )
        assert not result.success

    def test_store_none(self):
        result = asyncio.run(
            GetArtifactTool().execute(
                artifact_store=None,
                artifact_type="core.plain_text",
            )
        )
        assert not result.success

    def test_empty_store(self):
        result = asyncio.run(
            GetArtifactTool().execute(
                artifact_store=ArtifactStore(),
                artifact_type="core.plain_text",
            )
        )
        assert not result.success

    def test_label(self):
        store = ArtifactStore()
        store.put(_make("a1"))
        result = asyncio.run(
            GetArtifactTool().execute(
                artifact_store=store,
                artifact_type="core.plain_text",
                label="my_label",
            )
        )
        assert result.success
        assert result.metadata.get("label") == "my_label"
```

- [ ] **Step 2: 运行测试验证通过**

```bash
PYTHONPATH=. uv run python -m pytest tests/agent/tools/test_get_artifact.py -v
```

- [ ] **Step 3: Commit**

```bash
git add tests/agent/tools/test_get_artifact.py
git commit -m "test: add tests for get_artifact tool"
```

---

### Task 13: 删除旧测试文件并更新受影响测试

**Files:**
- Delete: `tests/agent/tools/test_read_cached.py`
- Delete: `tests/agent/tools/test_read_cached_runtime_policy.py`
- Delete: `tests/agent/test_read_cached_integration.py`
- Modify: 其余引用 `read_cached_output` 的测试文件

- [ ] **Step 1: 删除旧测试文件**

```bash
git rm tests/agent/tools/test_read_cached.py
git rm tests/agent/tools/test_read_cached_runtime_policy.py
git rm tests/agent/test_read_cached_integration.py
```

- [ ] **Step 2: Commit**

```bash
git commit -m "test: delete read_cached_output test files"
```

- [ ] **Step 3: 更新 tests/agent/test_loop.py 中的 read_cached_output 引用**

将 `test_read_cached_chain_blocked_by_loop_guard` 测试（第 280-290 行附近）删除或替换为 `get_artifact` 等效测试。由于我们已经删除了 `_is_read_cached_chain_call` 函数，这个测试不再相关。

删除 `tests/agent/test_loop.py` 中以 `test_read_cached` 开头的测试函数及相关 mock。

- [ ] **Step 4: 更新 tests/agent/test_context_manager.py 中的引用**

将 `test_read_cached_output_tool_message_is_not_repersisted` 测试（第 146-172 行附近）删除或替换。由于我们统一了 micro-compact 中的处理逻辑（不再有 read_cached_output 特殊分支），此测试需要更新为验证 `get_artifact` 类似行为（但 get_artifact 的 output_contract 为 debug_only，其 tool result 的 persist 行为由 ToolRegistry 控制）。

- [ ] **Step 5: 更新 tests/agent/test_tool_contract_binding.py 中的引用**

第 99-161 行的测试中使用了 `$ref:read_cached_output:1` 作为 debug artifact 示例。将其替换为其他 debug tool 的 ref（如 `$ref:debug_tool:1`）或直接删除相关断言行。

具体变更：
- 第 143 行：`ref_id="$ref:read_cached_output:1"` → 保留测试逻辑但使用不同的 test ref
- 第 145 行：`created_by="read_cached_output"` → `created_by="debug_tool"`  
- 第 160-161 行：断言保持逻辑不变

- [ ] **Step 6: 更新 tests/agent/test_cache_store.py 中的引用**

第 497-587 行涉及 `ReadCachedOutputTool` 的导入和测试。将这些测试删除或替换为针对 `GetArtifactTool` 的测试。`GetArtifactTool` 不会产生 `$ref:get_artifact:N` 引用（其结果为 `debug_only=True`），所以这些引用计数测试不再适用。

- [ ] **Step 7: 更新 tests/agent/artifacts 中的引用**

`tests/agent/artifacts/test_models.py:55` — 将 `created_by="read_cached_output"` 替换为 `created_by="debug_tool"`。
`tests/agent/artifacts/test_store.py:40,79,202` — 同上替换。
`tests/agent/artifacts/test_resolver.py:55,59` — 同上替换。

- [ ] **Step 8: 更新 tests/agent/agents/test_transform.py**

第 10-23 行涉及 `read_cached_output` 的断言。替换为 `list_artifacts` 和 `get_artifact`：

```python
def test_has_artifact_tools(self):
    agent = TransformAgent()
    tool_names = [t.name for t in agent._tools]
    assert "list_artifacts" in tool_names
    assert "get_artifact" in tool_names

def test_role_mentions_get_artifact(self):
    agent = TransformAgent()
    assert "get_artifact" in agent.role
```

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "test: update all tests referencing read_cached_output"
```

---

### Task 14: 运行全量测试并修复

- [ ] **Step 1: 运行全量测试**

```bash
uv run pytest tests/ -v --tb=short 2>&1 | tail -80
```

- [ ] **Step 2: 修复所有失败测试**

针对每个失败测试逐一修复，确保所有测试通过。

- [ ] **Step 3: 最终 commit**

```bash
git add -A
git commit -m "test: fix remaining test failures after artifact tools migration"
```

---

### Task 15: 验证 — 运行 app 确认端到端正常

- [ ] **Step 1: 启动应用并测试**

```bash
# 启动应用
uv run python -m src.main &
# 等待启动后测试 /api/audit 端点（使用 existing test document）
```

- [ ] **Step 2: 确认审计流程正常运行**

验证 orchestrator 能正确调度子 agent，artifact 自动绑定正常工作，
`list_artifacts` 和 `get_artifact` 在 agent 中可用。

- [ ] **Step 3: Commit 最终调整**

如有问题，修复并 commit。
```
