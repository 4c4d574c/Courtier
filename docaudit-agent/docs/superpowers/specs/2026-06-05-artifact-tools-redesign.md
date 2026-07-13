# Artifact 工具重构：list_artifacts + get_artifact 替换 read_cached_output

Date: 2026-06-05
Status: 设计已批准，待实现

## 1. 问题陈述

`read_cached_output` 是一个调试工具，从磁盘 JSON 缓存读取原始数据，支持 schema 查看和 jq 字段提取。它在设计上有根本限制：

1. 操作的是**无类型的原始缓存**（`$ref:tool:N`），不感知 artifact 的语义角色和类型
2. 需要 LLM 手动编写 jq 表达式来提取/重塑数据，这正是 artifact-projector 框架设计要消除的模式
3. 输出标记为 `core.debug_view`（`debug_only=True`），被排除在业务数据流之外
4. 子 agent（如 plagiarism auditor）用它来探索缓存结构和提取文本，消耗大量轮次却没有调用真正的业务工具

同时，artifact-projector 框架已经成熟：类型化 artifact 注册、BFS 投影路径解析、链式投影执行、物化器。但 LLM **没有任何方式主动使用这些能力** —— 投影器只在 `ToolRegistry._bind_contract_arguments()` 中自动调用，对 LLM 完全透明。

## 2. 设计目标

让 LLM 可以直接与 artifact/projector 系统交互：
- 浏览当前可用的类型化 artifact
- 按需请求投影并获取物化后的数据
- 消除手动 jq 探索的需求
- 保留现有的自动绑定路径不变

## 3. 方案选择

**选择方案 2（富语义浏览 + 精确投影）**：两个独立工具 —— `list_artifacts` 和 `get_artifact`。

`read_cached_output` **直接删除**，不做渐进废弃。

## 4. 工具接口

### 4.1 list_artifacts

```
描述：列出 artifact store 中当前可用的类型化数据工件，包括类型、语义角色、
      主题、来源、质量信息。用于在调用 get_artifact 之前了解有哪些数据可用。

参数：
  artifact_type  可选  string  按类型过滤（如 "core.plain_text"）
  role           可选  string  按语义角色过滤
                              （"document"|"reference"|"intermediate"|"debug"）
  subject        可选  string  按主题过滤（如 "current"、"search_results"）

返回：
  {
    "artifacts": [
      {
        "artifact_id": "...",
        "artifact_type": "core.plain_text",
        "role": "document",
        "subject": "current",
        "created_by": "parse_document",
        "content_hash": "sha256:...",
        "quality": {"confidence": 0.95},
        "lineage": ["docaudit.parsed_document.to_paragraph_list", 
                     "docaudit.paragraph_list.to_plain_text"]
      }
    ],
    "count": 1,
    "hint": "使用 get_artifact 按需获取投影后的数据"
  }
```

运行时策略：
- 无调用限制（轻量只读）
- 对所有 agent（含子 agent）可见
- 输出不进入 artifact store

### 4.2 get_artifact

```
描述：从 artifact store 获取指定类型的数据，自动执行所需的类型投影。
      可以直接指定 artifact_id，或让系统根据类型和约束自动选择最佳来源。

参数：
  artifact_type  必填  string  目标类型（如 "core.plain_text"、"core.text_collection"）
  artifact_id    可选  string  指定具体 artifact（跳过自动选择）
  role           可选  string  指定语义角色（如不指定则系统选择最佳匹配）
  subject        可选  string  指定主题（如不指定则系统选择最佳匹配）
  constraints    可选  object  传递给 projector 的约束
                              （如 {"source_scope": "body", "max_chars": 5000}）
  materialize_as 可选  string  物化方式（如不指定则用默认值）
  label          可选  string  为结果添加语义标签

返回（成功）：
  {
    "value": <物化后的具体数据>,
    "artifact_id": "投影链最终 artifact 的 ID",
    "artifact_type": "core.plain_text",
    "trace": {
      "field": "...",
      "source_artifact": "...",
      "steps": [{"projector": "...", "cache": "hit|miss"}],
      "materializer": {"name": "...", "path": "...", "output_type": "string"}
    },
    "metadata": {"content_hash": "...", "quality": {...}, "lineage": [...]}
  }

返回（失败）：
  {
    "error": "未找到匹配的 artifact，建议先调用 parse_document 生成 parsed_document",
    "suggestions": [{"action": "call_tool", "tool": "parse_document", "reason": "..."}]
  }
```

运行时策略：
- `max_calls_per_agent_run=30`、`max_consecutive_calls=5`、`allow_chaining=False`
- 对所有 agent（含子 agent）可见
- 结果标记为 `core.debug_view`（debug_only=True），不进入业务 artifact store；label 仅影响返回值标记

### 4.3 行为场景

**场景 A — 自动选择：**
```
get_artifact(artifact_type="core.plain_text", role="document",
             constraints={"source_scope": "body", "max_chars": 5000})
  → Resolver BFS 搜索: parsed_document → paragraph_list → plain_text
  → Executor 执行 2 步投影链
  → Materializer: core.plain_text.string → "第一章 总则..."
  → 返回 {value, artifact_id, trace}
```

**场景 B — 指定 artifact：**
```
get_artifact(artifact_type="core.text_collection",
             artifact_id="projected:core.text_collection:def456abc123")
  → 跳过 Resolver，直接对指定 artifact 执行投影（已是目标类型则零步投影）
```

**场景 C — 无匹配：**
```
get_artifact(artifact_type="docaudit.audit_finding_list")
  → ArtifactStore 中无此类型
  → Resolver 返回 failed + upstream_producer_for() 建议
  → 返回 error + suggestions
```

## 5. 实现架构

### 5.1 文件变更

```
新增：
  src/agent/tools/builtin/list_artifacts.py   — ListArtifactsTool
  src/agent/tools/builtin/get_artifact.py     — GetArtifactTool

删除：
  src/agent/tools/builtin/read_cached.py

修改：
  src/agent/tools/registry.py                  — 注册新工具；清理 read_cached_output 特有逻辑
  src/agent/agents/base.py                     — AuditorAgent 使用新工具替换 ReadCachedOutputTool
  src/agent/agents/transform.py                — 移除 ReadCachedOutputTool 导入和引用
  src/agent/agents/orch.py                     — 移除 ReadCachedOutputTool 导入和引用
  src/agent/core/loop.py                       — 更新探索性工具追踪和 ref 指令
  src/agent/core/context_manager.py            — 移除 read_cached_output 引用
  src/agent/artifacts/models.py                — hide_read_cached_output_for_task_agents
                                                   → hide_debug_tools_for_task_agents

测试：
  删除 tests/agent/tools/test_read_cached.py
  删除 tests/agent/tools/test_read_cached_runtime_policy.py
  删除 tests/agent/test_read_cached_integration.py
  新增 tests/agent/tools/test_list_artifacts.py
  新增 tests/agent/tools/test_get_artifact.py
  更新其余测试中的 read_cached_output 引用
```

### 5.2 核心逻辑复用

```
list_artifacts:
  artifact_store.list_projection_candidates()
    → 按 type/role/subject 过滤
    → 排除 debug_only=True 的 artifact
    → 提取公开元数据（不含原始 data）
    → 返回列表

get_artifact:
  if artifact_id 指定:
    source = artifact_store.require(artifact_id)
    构建单字段 ContractField(type, role, subject, constraints)
  else:
    ProjectionResolver.resolve(单字段 contract, candidates, policy)
      → BFS 搜索最佳投影路径
      → 失败时通过 upstream_producer_for() 生成建议

  ProjectionExecutor.execute(plan)
    → 链式执行 projector（带步骤缓存）
    → MaterializerRegistry.materialize()
    → validate_materialized_value()

  返回 {value, artifact_id, trace, metadata}
```

关键原则：
- **复用而非重建**：Resolver、Executor、MaterializerRegistry 全部复用
- **只读查询**：get_artifact 结果不进入 artifact store
- **共享实例**：tool 使用 agent loop 传入的 ArtifactStore 实例

## 6. 安全策略

| 约束 | 措施 |
|------|------|
| debug_only artifact 不参与自动选择 | list_artifacts 排除；get_artifact 自动选择排除（显式 artifact_id 可访问） |
| 子 agent 隔离 | 沿用 ArtifactContext + ArtifactPermission，子 agent 仅见被允许的 artifact |
| 结果不进 store | 只读查询，不产生新 artifact 链 |
| 调用频率 | get_artifact max_consecutive_calls=5 |
| 链式调用 | get_artifact allow_chaining=False |

## 7. 非目标

- 不删除或替换现有的 ContextManager/ref 系统
- 不让 get_artifact 自动触发上游工具调用（只建议，不执行）
- 不在 list_artifacts 中返回 artifact 的原始数据
- 不在 get_artifact 中暴露 jq 查询能力
- 不修改 projector 注册表或 resolver 的核心逻辑
