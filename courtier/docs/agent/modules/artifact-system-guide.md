# Artifact 模块详解

> **模块路径:** `src/agent/artifacts/`
> **定位:** 类型化数据传递与自动转换中间层
> **最后更新:** 2026-07-04

---

## 目录

1. [模块定位与核心概念](#1-模块定位与核心概念)
2. [文件结构](#2-文件结构)
3. [核心数据模型](#3-核心数据模型)
4. [ArtifactStore — 存储与生命周期](#4-artifactstore--存储与生命周期)
5. [投影系统 — 类型转换图引擎](#5-投影系统--类型转换图引擎)
6. [Artifact 生命周期](#6-artifact-生命周期)
7. [与各模块的联动](#7-与各模块的联动)
8. [完整数据流示例](#8-完整数据流示例)
9. [设计亮点总结](#9-设计亮点总结)

---

## 1. 模块定位与核心概念

### 1.1 解决什么问题？

在 DocAudit 系统中，不同的 Tool、Plugin、Skill 操作不同类型的数据：

- `parse` 工具产出 `docaudit.parsed_document`（解析后的文档结构）
- `search` 工具产出 `docaudit.search_results`（搜索结果）
- `detect_plagiarism` 工具需要 `core.plain_text` + `core.text_collection`

**这些类型不一致，工具之间如何传递数据？**

Artifact 模块的答案：**声明式契约 + 自动类型投影**。让每个工具声明"我能产出什么"和"我需要什么"，系统自动在图谱中找到类型转换路径并执行。

### 1.2 核心抽象一览

| 概念 | 说明 |
|------|------|
| **Artifact**（工件） | 有类型、有 Schema、有元数据的不可变数据容器 |
| **ArtifactType**（工件类型） | 如 `core.plain_text`、`docaudit.parsed_document`，带版本化的 JSON Schema |
| **ArtifactStore**（工件存储） | 会话级的内存存储，管理工件生命周期 |
| **Projector**（投影器） | 将一个 ArtifactType 转换为另一个的有向边 |
| **ProjectionPlan**（投影计划） | BFS 图搜索找到的类型转换路径（多步 Projector 链） |
| **Materializer**（物化器） | 将最终 Artifact 转换为工具可接收的参数形式 |
| **InputField**（输入契约） | 工具声明"我需要什么类型，以什么形式接收" |
| **ContractBinder**（契约绑定器） | 连接 ToolRegistry 和 ArtifactStore，自动绑定参数 |

---

## 2. 文件结构

```
src/agent/artifacts/
├── __init__.py       # 公开 API，导出 35 个公开符号
├── models.py         # 788 行 — Pydantic 模型、枚举、Schema 注册、ArtifactRef 解析
├── store.py          # 188 行 — ArtifactStore + ScopedArtifactStore（权限隔离）
├── resolver.py       # 268 行 — ProjectionResolver（BFS 图搜索投影路径）
├── executor.py       # 275 行 — ProjectionExecutor + MaterializerRegistry
├── projectors.py     # 486 行 — Projector + ProjectorRegistry + 默认投影函数
├── binder.py         # 111 行 — ContractBinder（ToolRegistry ↔ ArtifactStore 协调器）
└── registry.py       #  32 行 — SessionArtifactStoreRegistry（session_id → store 映射）
```

---

## 3. 核心数据模型

### 3.1 Artifact — 不可变数据容器

```python
# models.py:388
class Artifact(BaseModel, frozen=True):
    artifact_id: str           # 唯一标识
    artifact_type: str         # 类型名，如 "core.plain_text"
    schema_version: str        # Schema 版本号
    data: Any                  # 实际数据体
    metadata: ArtifactMetadata # 元信息
```

**artifact_id 命名规则：**

| 模式 | 示例 | 来源 |
|------|------|------|
| `$ref:{tool_name}:latest` | `$ref:parse:latest` | 工具输出注册 |
| `$ref:{tool_name}:{n}` | `$ref:search:3` | 多次调用序号 |
| `projected:{type}:{hash[-12:]}` | `projected:plain_text:a1b2c3d4e5f6` | 投影操作产物 |

**ArtifactMetadata 字段：**

```python
# models.py:371
class ArtifactMetadata(BaseModel, frozen=True):
    created_by: str = ""           # 创建者（工具名/投影器名）
    source_refs: tuple[str, ...]   # 来源工件引用
    content_hash: str = ""         # SHA-256 内容哈希
    semantic_role: str = ""        # 语义角色标记（如 "document", "reference"）
    subject: str = ""              # 主题标记
    scope: str = ""                # 作用范围
    sensitivity: str = "internal"  # 敏感度
    projection_allowed: bool = True
    debug_only: bool = False       # 仅调试，不参与业务推理
    quality: float = 1.0           # 质量评分 0~1
    lineage: tuple[str, ...] = ()  # 来源谱系
```

### 3.2 ArtifactSchema — 类型 Schema 注册

系统在 `_build_artifact_type_schemas()` 中预注册了 **15 种内置 Schema**（`models.py:46-305`），分为两层：

#### Core 层（通用类型）

| 类型 | 说明 | 关键字段 |
|------|------|---------|
| `core.plain_text` | 纯文本 | `text`, `language`, `source_scope` |
| `core.text_collection` | 有序文本集合 | `items: [{text, metadata}]` |
| `core.json_object` | 任意 JSON | 无约束 |
| `core.error_report` | 结构化错误报告 | `errors: [{code, message, location}]` |
| `core.debug_view` | 调试缓存视图 | 标记为 `debug_only` |

#### DocAudit 领域层（业务类型）

| 类型 | 说明 | 关键字段 |
|------|------|---------|
| `docaudit.parsed_document` | 解析后的文档 | `pages: [{page_num, elements}]` |
| `docaudit.paragraph_list` | 有序段落列表 | `paragraphs: [{text, metadata}]` |
| `docaudit.search_results` | 搜索结果 | `total`, `hits: [{text, score, source}]` |
| `docaudit.document_metadata` | 文档元信息 | `doc_id`, `title`, `author`, `date` |
| `docaudit.document_structure` | 文档大纲结构 | `sections: [{title, level, children}]` |
| `docaudit.audit_finding_list` | 审计发现列表 | `findings: [{severity, description, location}]` |
| `docaudit.audit_report` | 审计报告 | `summary`, `findings`, `suggestions` |
| `docaudit.reference_text_list` | 参考文本列表 | `references: [{text, source, relevance}]` |
| `docaudit.plagiarism_report` | 抄袭检测报告 | `matches: [{source_text, target_text, similarity}]` |

#### 上游生产者映射

```python
# models.py:523
_UPSTREAM_PRODUCERS: dict[str, list[str]] = {
    "docaudit.parsed_document":    ["parse"],
    "docaudit.search_results":     ["search"],
    "docaudit.audit_report":       ["format_audit", "content_audit", "style_audit"],
    "docaudit.plagiarism_report":  ["detect_plagiarism"],
    "docaudit.audit_finding_list": ["format_audit", "content_audit"],
    # ...
}
```

这个映射让系统知道"要获得某类型数据，应该调用哪些工具"，用于终端工具就绪检测。

### 3.3 InputField — 工具的输入契约

```python
# models.py:564
@dataclass(frozen=True)
class InputField:
    name: str                # 参数名
    artifact_type: str       # 期望的 artifact 类型
    materialize_as: str      # 物化形式："string" | "dict" | "list_string" | "list_dict"
    required: bool = True    # 是否必填
    constraints: dict = {}   # 约束条件
```

**`materialize_as` 的值含义：**

| 值 | 说明 | 示例 |
|----|------|------|
| `"string"` | 提取为单个字符串 | `data["text"]` → `"xxx"` |
| `"dict"` | 整个 data 作为字典 | `data` → `{...}` |
| `"list_string"` | 提取为字符串列表 | `data["items"]` → `["a", "b"]` |
| `"list_dict"` | 提取为字典列表 | `data["findings"]` → `[{...}, {...}]` |
| `"$.path.to.field"` | JSONPath 路径提取 | `data["a"]["b"]` → value |

#### 工具声明示例

以抄袭检测插件为例（`plugins/audit/plagiarism/tools.py:47-57`）：

```python
input_fields = (
    InputField(
        name="document",
        artifact_type="core.plain_text",
        materialize_as="string",
        required=True,
    ),
    InputField(
        name="references",
        artifact_type="core.text_collection",
        materialize_as="list_string",
        required=True,
    ),
)
```

### 3.4 ProjectionPolicy — 投影策略

```python
# models.py:496
class ProjectionPolicy(BaseModel, frozen=True):
    max_depth: int = 4               # BFS 最大搜索深度
    min_quality: float = 0.7         # 路径最低质量阈值
    allow_lossy: bool = True         # 是否允许有损转换
    prefer_cached: bool = True       # 优先使用匹配类型的缓存结果
    allow_debug_artifacts: bool = False
    features: ProjectionFeatureFlags # 特性开关
```

### 3.5 枚举与字面量类型

```python
# models.py:12-15
ProjectorStability = Literal["stable", "experimental", "deprecated", "disabled"]
ProjectorLayer     = Literal["core", "domain", "plugin", "local"]
ProjectorCost      = Literal["free", "cheap", "moderate", "expensive"]
Lossiness          = Literal["lossless", "lossy", "summary"]
```

---

## 4. ArtifactStore — 存储与生命周期

### 4.1 ArtifactStore（主存储）

```python
# store.py:13
class ArtifactStore:
    _artifacts: dict[str, Artifact]  # artifact_id → Artifact
    _type_policies: dict[str, tuple[bool, bool]]  # type → (persist, llm_visible)
```

**核心方法：**

| 方法 | 说明 |
|------|------|
| `put(artifact)` | 存储工件，按类型策略控制持久化 |
| `get(artifact_id)` / `require(artifact_id)` | 读取（不存在返回 None / 抛异常） |
| `list_all()` / `list_visible()` / `list_projection_candidates()` | 列出工件，带过滤 |
| `find_by_type(artifact_type)` | 按类型查找 |
| `register_cached_ref(tool_name, artifact_type, data, ...)` | 从原始数据创建工件 |
| `set_type_policy(artifact_type, persist, llm_visible)` | 按类型设置持久化策略 |

**类型策略控制：**
- `put()` 时检查该类型的 `(persist, llm_visible)` 策略
- `persist=False` 的类型，新写入会覆盖旧值（不累积）
- `debug_only=True` 的工件不在 `list_visible()` 中出现

### 4.2 ScopedArtifactStore（子代理隔离）

```python
# store.py:107
class ScopedArtifactStore:
    """包装 ArtifactStore + ArtifactContext，为子代理提供权限隔离"""
```

**权限控制矩阵：**

| 操作 | 未授权工件 | 已授权工件 (project=True) | 已授权工件 (materialize=True) |
|------|-----------|-------------------------|------------------------------|
| `get()` | ❌ 不可见 | ✅ 可读 | ✅ 可读 |
| `list_*()` | ❌ 过滤掉 | ✅ 出现在列表中 | ✅ 出现在列表中 |
| 投影 (project) | ❌ | ✅ | ✅ |
| 物化 (materialize) | ❌ | ❌ | ✅ |
| `put()` | ✅ 始终允许 | ✅ | ✅ |
| 调试工件 | ❌ 默认排除 | ❌ | ❌ |

### 4.3 SessionArtifactStoreRegistry

```python
# registry.py
class SessionArtifactStoreRegistry:
    _stores: dict[str, ArtifactStore]  # session_id → ArtifactStore
    _lock: asyncio.Lock                # 线程安全
```

**生命周期：**
- 在 `app.py:128` 创建，挂载到 `app.state.artifact_store_registry`
- 新会话开始时 `register(session_id, store)`
- 会话结束时 `unregister(session_id)`
- Plugin 系统通过此注册表按 `session_id` 路由到正确的 Store

---

## 5. 投影系统 — 类型转换图引擎

投影系统是 Artifact 模块最核心的能力：**在不同 ArtifactType 之间自动找到转换路径并执行**。

### 5.1 Projector（投影器）

```python
# projectors.py:22
class Projector:
    spec: ProjectorSpec       # 元数据声明
    _fn: Callable             # 投影函数
```

**ProjectorSpec 字段：**

```python
# models.py:419
class ProjectorSpec(BaseModel, frozen=True):
    name: str            # 唯一名称
    source_type: str     # 源类型
    target_type: str     # 目标类型
    version: str         # 版本
    owner: str           # 所属模块
    layer: ProjectorLayer
    stability: ProjectorStability
    lossiness: Lossiness
    cost: ProjectorCost
    quality_score: float # 0~1
    description: str
```

#### 内置投影器图

系统注册了 **4 个默认投影器**（`projectors.py:188`），构成以下类型转换图：

```
                        0.95 (lossy)
docaudit.parsed_document ───────────▶ docaudit.paragraph_list
                                              │
                                              │ 0.98 (lossy)
                                              ▼
                                       core.plain_text


                        0.92 (lossy)
docaudit.search_results ───────────▶ docaudit.reference_text_list
                                              │
                                              │ 0.97 (lossy)
                                              ▼
                                       core.text_collection
```

| 投影器名称 | 源 → 目标 | 质量 | 说明 |
|-----------|----------|------|------|
| `parsed_document.to_paragraph_list` | `docaudit.parsed_document` → `docaudit.paragraph_list` | 0.95 | 从文档页面提取段落 |
| `paragraph_list.to_plain_text` | `docaudit.paragraph_list` → `core.plain_text` | 0.98 | 拼接段落为纯文本 |
| `search_results.to_reference_text_list` | `docaudit.search_results` → `docaudit.reference_text_list` | 0.92 | 过滤去重搜索结果 |
| `reference_text_list.to_text_collection` | `docaudit.reference_text_list` → `core.text_collection` | 0.97 | 转为通用文本集合 |

### 5.2 ProjectorRegistry（投影器注册表）

```python
# projectors.py:88
class ProjectorRegistry:
    _by_name: dict[str, Projector]
    _by_source: dict[str, list[Projector]]  # source_type → [projectors]
    _by_target: dict[str, list[Projector]]  # target_type → [projectors]
    _by_edge: dict[tuple[str, str], Projector]  # (source, target) → projector
```

**核心方法：**

| 方法 | 说明 |
|------|------|
| `register(projector)` | 注册投影器，检测重复边 |
| `outgoing(source_type)` | 获取某类型的所有出边 |
| `by_target(target_type)` | 获取指向某类型的所有入边 |
| `by_edge(source, target)` | 获取特定边的投影器 |
| `by_layer(layer)` | 按层级过滤 |
| `promote(name, new_layer)` | 提升投影器层级 |
| `graph_summary()` | 图审计：列出所有类型、死胡同、可达性 |

### 5.3 ProjectionResolver — BFS 路径搜索

```python
# resolver.py:33
class ProjectionResolver:
    _registry: ProjectorRegistry
```

**核心流程：**

```
1. 对每个 missing_field：
   ├── 遍历 ArtifactStore 中的候选工件（projection_candidates）
   ├── 检查类型是否完全匹配（零步投影：artifact_type == field.artifact_type）
   ├── 如果不匹配，BFS 搜索从 artifact_type 到 field.artifact_type 的最短路径
   └── 返回所有有效 ProjectionPlan（按质量评分排序）

2. BFS 搜索（_find_shortest_path，resolver.py:125）：
   ├── 队列初始化为 [(source_type, [])]
   ├── 每步：
   │   ├── 取当前类型的所有 outgoing projectors
   │   ├── 过滤：跳过 disabled/deprecated/experimental
   │   ├── 过滤：质量 < min_quality
   │   ├── 过滤：不允许有损且 projector 是有损的
   │   ├── 过滤：Schema 版本不兼容
   │   ├── 过滤：超过 max_depth
   │   └── 到达目标类型 → 构建 ProjectionPlan
   └── 选择最优计划（零步 > 最短路径 > 最高质量）
```

**BFS 搜索示例：**

```
Store 中有: docaudit.parsed_document
Field 需要: core.plain_text (as string)

BFS:
  Level 0: [parsed_document]
  Level 1: [paragraph_list]          ← 通过 parsed_document.to_paragraph_list
  Level 2: [plain_text] ✓ 找到！     ← 通过 paragraph_list.to_plain_text

结果: 2-step plan，累积质量 ≈ 0.95 × 0.98 = 0.931
```

### 5.4 ProjectionExecutor — 执行引擎

```python
# executor.py:131
class ProjectionExecutor:
    _projector_registry: ProjectorRegistry
    _materializer_registry: MaterializerRegistry
    _artifact_store: ArtifactStore
```

**执行流程（executor.py:170）：**

```
对 Plan 中的每个 projection_step：
  ├── 1. 计算内容哈希 → 检查步骤缓存（相同输入+投影器=相同输出）
  ├── 2. projector.project(artifact, constraints)
  │     ├── 验证源类型匹配
  │     ├── 执行投影函数
  │     ├── 验证输出 Schema
  │     └── 创建新 Artifact（frozen）
  ├── 3. artifact_store.put(projected_artifact)
  ├── 4. 记录 ProjectionTraceStep
  └── 5. 当前 artifact = 投影结果（传递给下一步）

最后一步：
  └── MaterializerRegistry.materialize(final_artifact, materializer_spec)
      → 返回 MaterializedBinding(value, artifact_id, trace)
```

### 5.5 MaterializerRegistry — 物化器

```python
# executor.py:18
class MaterializerRegistry:
    _materializers: dict[tuple[str, str], Callable]  # (artifact_type, materialize_as) → fn
```

**内置物化映射（executor.py:24）：**

```python
{
    ("core.plain_text",      "string"):       lambda a: a.data["text"],        # → str
    ("core.plain_text",      "dict"):          lambda a: a.data,               # → dict
    ("core.text_collection", "list_string"):   lambda a: [i["text"] for i in a.data["items"]],
    ("*",                    "dict"):          lambda a: a.data,               # 兜底
    ("*",                    "string"):        lambda a: json.dumps(a.data),   # 兜底
}
```

也支持 JSONPath 路径提取：`materialize_as="$.findings[*].description"`

---

## 6. Artifact 生命周期

### 6.1 创建路径

```
工具输出注册（主要路径）
  │
  ├── ToolRegistry.execute() 成功后
  │   └── _register_output_artifact_simple()          # registry.py:324
  │       ├── 构建 artifact_id: "$ref:{tool_name}:latest"
  │       ├── artifact_store.register_cached_ref(...)  # store.py:77
  │       └── emit "artifact_created" 事件
  │
投影产物（中间路径）
  │
  └── ProjectionExecutor.execute()
      └── projector.project()
          └── 创建 Artifact(artifact_id="projected:{type}:{hash[-12:]}", ...)

Plugin 写入（外部路径）
  │
  └── PluginManager._handle_artifact_store_put()
      └── SessionArtifactStoreRegistry.get(session_id).register_cached_ref(...)
```

### 6.2 消费路径

```
直接获取（显式）
  │
  ├── get_artifact 工具被 LLM 调用
  │   └── 运行完整 resolver → executor → materializer 管线
  │
自动绑定（隐式，核心路径）
  │
  └── ToolRegistry.execute()
      └── ContractBinder.bind_tool_inputs()
          ├── 识别缺失必填字段
          ├── ProjectionResolver.resolve()
          ├── ProjectionExecutor.execute()
          └── 注入到工具 kwargs

子代理消费（隔离）
  │
  └── AgentRuntime.delegate()
      └── ScopedArtifactStore（权限过滤包装）
```

### 6.3 清理

- `debug_only=True` 的工件不出现在 `list_visible()` 中
- `persist=False` 类型的新工件覆盖旧值
- 会话结束 → `SessionArtifactStoreRegistry.unregister(session_id)`

---

## 7. 与各模块的联动

### 7.1 架构总览

```
                          ┌──────────────────┐
                          │   Orchestrator   │
                          │   Agent (orch)   │
                          └────────┬─────────┘
                                   │ 委派
                          ┌────────▼─────────┐
                          │  AgentRuntime    │
                          │  .delegate()     │
                          │  _build_scoped   │
                          │  _store()        │
                          └────────┬─────────┘
                                   │
              ┌────────────────────┼────────────────────┐
              │                    │                    │
     ┌────────▼────────┐  ┌───────▼────────┐  ┌────────▼────────┐
     │  ScopedStore A  │  │  ScopedStore B │  │  ScopedStore C  │
     │  (子代理1)       │  │  (子代理2)     │  │  (子代理3)      │
     └────────┬────────┘  └───────┬────────┘  └────────┬────────┘
              │                    │                    │
              └────────────────────┼────────────────────┘
                                   │ 结果合并
                          ┌────────▼─────────┐
                          │   ArtifactStore  │  ← 主 Store
                          │   (会话级单例)    │
                          └────────┬─────────┘
                                   │
              ┌────────────────────┼────────────────────┐
              │                    │                    │
     ┌────────▼────────┐  ┌───────▼────────┐  ┌────────▼────────┐
     │  ToolRegistry   │  │ ContractBinder │  │  PluginManager  │
     │  .execute()     │  │ (自动绑定)      │  │  (主机服务)      │
     └────────┬────────┘  └───────┬────────┘  └────────┬────────┘
              │                    │                    │
              │           ┌───────▼────────┐           │
              │           │ProjectionResolver│          │
              │           │  (BFS 搜索)     │          │
              │           └───────┬────────┘           │
              │                    │                    │
              │           ┌───────▼────────┐           │
              │           │ProjectionExecutor│         │
              │           │  (执行+物化)    │          │
              │           └───────┬────────┘           │
              │                    │                    │
              └────────────────────┼────────────────────┘
                                   │
                          ┌────────▼─────────┐
                          │   Agent Loop     │
                          │   (消费+监控)     │
                          └──────────────────┘
```

### 7.2 ToolRegistry ↔ ContractBinder ↔ ArtifactStore

这是最核心的自动绑定链路，发生在 `ToolRegistry.execute()` 中（`src/agent/tools/registry.py:159-185`）：

```
1. LLM 决定调用工具，传入部分参数（explicit_kwargs）
       │
2. ToolRegistry 检查工具的 input_fields 契约
       │
3. ContractBinder.bind_tool_inputs(fields, tool_name, explicit_kwargs)
       │
       ├── 识别缺失的必填字段（不在 explicit_kwargs 中的 required 字段）
       │
       ├── ProjectionResolver.resolve(missing_fields, tool_name, candidates, policy)
       │   ├── 遍历 Store 中的每个候选工件
       │   ├── BFS 搜索从 artifact_type 到 field.artifact_type 的最短路径
       │   └── 返回 ProjectionResolution（含 plans 或 diagnostics）
       │
       ├── 如果解析失败 → 返回 ToolResult(success=False, error="...")
       │
       ├── ProjectionExecutor.execute(plan) 逐个 plan 执行
       │   ├── 执行投影链
       │   ├── 物化为工具参数
       │   └── 返回 MaterializedBinding(value, artifact_id, trace)
       │
       ├── validate_materialized_value(binding.value, field)
       │   └── 校验约束条件（max_chars, max_items 等）
       │
       └── 返回 ToolResult(success=True, data={
               arguments: {name: value, ...},
               artifact_bindings: {name: artifact_id, ...}
           })
       │
4. 合并 explicit_kwargs + bound arguments → 执行工具
       │
5. 工具成功 → _register_output_artifact_simple()
   └── 将结果注册回 ArtifactStore（形成闭环）
```

### 7.3 Agent Loop 中的三个控制点

`agent_loop()` 接收 `artifact_store`，用于三个关键控制：

#### a) 进度追踪

```python
# loop_guards.py:90
check_business_artifact_progress(artifact_store, ...)
```

- 每轮循环检查是否有**新的非 debug 业务工件**产生
- 连续 `loop_max_turns_without_business_artifacts` 轮无新工件 → 终止循环
- 防止 Agent 陷入无意义的空转

#### b) 终端工具就绪检测

```python
# loop_hints.py:132
check_and_inject_hints(artifact_store, tool_registry, ...)
```

- 扫描所有工具的 `input_fields` 契约
- 检查哪些工具的所需类型已在 ArtifactStore 中可解析
- 向 LLM 注入中文提示：
  - "以下终端工具已就绪，请考虑调用：`generate_report`"
  - "以下工具还缺少前置数据：`detect_plagiarism` 缺少 `core.plain_text`"

#### c) 探索循环检测

```python
# loop_guards.py:18
check_explore_loop(artifact_store, tool_call_history, ...)
```

- `get_artifact` 和 `list_artifacts` 被标记为探索性工具
- 检测是否陷入反复探索而无实质进展

### 7.4 AgentRuntime — 子代理隔离

```python
# runtime.py:200
AgentRuntime.delegate(...)
```

**委派流程：**

```
1. 遍历主 ArtifactStore 中所有非 debug 工件
2. 为每个工件创建 ArtifactContextEntry(artifact_id, permissions)
3. 构建 ArtifactContext(allowed_artifacts=[...], allow_all=False)
4. 创建 ScopedArtifactStore(store, context) → 权限过滤包装
5. 组装 AgentConfig，注入 scoped_store
6. 子代理在自己的 ScopedStore 中运行
7. 子代理返回 → _collect_subagent_artifacts() 合并回主 Store
```

### 7.5 Plugin 系统集成

Plugin 以独立 TCP 服务运行，通过换行分隔 JSON-RPC 与主进程通信。

**可用的主机服务方法（`src/plugin/manager.py:468-524`）：**

| 方法 | 功能 | 权限 |
|------|------|------|
| `METHOD_ARTIFACT_STORE_PUT` | 写入工件（构建 Artifact 后 put） | `write:artifacts` |
| `METHOD_ARTIFACT_STORE_GET` | 按 artifact_id 读取 | `read:artifacts` |
| `METHOD_ARTIFACT_STORE_LIST` | 列出所有工件 | `read:artifacts` |

**Plugin 如何声明契约：**

在 `plugin.yaml` 中，Plugin 工具声明：

```yaml
tools:
  - name: detect_plagiarism
    output_artifact_type: docaudit.plagiarism_report
    input_fields:
      - name: document
        artifact_type: core.plain_text
        materialize_as: string
      - name: references
        artifact_type: core.text_collection
        materialize_as: list_string
```

`PluginManager` 将这些加载为 `ProxyTool` 进入 `ToolRegistry`，之后它们和内置工具享有完全相同的 ContractBinder 自动绑定能力。

### 7.6 内置工具

#### list_artifacts

`src/agent/tools/builtin/list_artifacts.py`（144 行）

- 列出 ArtifactStore 中所有可见工件
- 返回每个工件的 id、类型、语义角色、主题、质量、谱系
- 附带 `projectable_to_types` —— 告诉 LLM 哪些类型转换路径可用

#### get_artifact

`src/agent/tools/builtin/get_artifact.py`（259 行）

- 按 ID 获取工件
- 运行完整 resolver → executor → materializer 管线
- `runtime_policy = RuntimePolicy(max_calls=30, max_consecutive=5)` — 限流防死循环
- 防御性检查：拒绝 `$ref:get_artifact:*` 的循环引用
- 输出 `core.debug_view` 类型（避免自身成为业务工件被追踪）

### 7.7 OrchestratorAgent 集成

```python
# orch.py:61-64
self.register_tool(ListArtifactsTool())
self.register_tool(GetArtifactTool())
```

- System prompt 中包含 `$ref:{tool}:{n}` 引用格式的使用说明
- 委派子代理时传递 `artifact_context`，让子代理知道有哪些可用数据

---

## 8. 完整数据流示例

以"抄袭检测"审计场景为例，展示端到端的 Artifact 流转：

```
┌──────────────────────────────────────────────────────────────────┐
│ Step 1: parse 工具执行                                            │
│                                                                   │
│ Tool: plugins/common/parse/tools.py                               │
│ output_artifact_type = "docaudit.parsed_document"                │
│                                                                   │
│ → _register_output_artifact_simple("parse", ...)                 │
│ → ArtifactStore.put(                                              │
│     Artifact(                                                     │
│       artifact_id="$ref:parse:latest",                           │
│       artifact_type="docaudit.parsed_document",                  │
│       data={pages: [{page_num: 1, elements: [...]}, ...]},       │
│       metadata=ArtifactMetadata(                                  │
│         created_by="parse", semantic_role="document",            │
│         quality=1.0, debug_only=False                            │
│       )                                                           │
│     )                                                             │
│   )                                                               │
├──────────────────────────────────────────────────────────────────┤
│ Step 2: search 工具执行                                            │
│                                                                   │
│ Tool: plugins/common/search/tools.py                              │
│ output_artifact_type = "docaudit.search_results"                 │
│                                                                   │
│ → ArtifactStore.put(                                              │
│     Artifact(                                                     │
│       artifact_id="$ref:search:latest",                          │
│       artifact_type="docaudit.search_results",                   │
│       data={total: 15, hits: [{text: "...", score: 0.9}, ...]}, │
│       metadata=ArtifactMetadata(                                  │
│         created_by="search", semantic_role="references",         │
│         quality=1.0                                               │
│       )                                                           │
│     )                                                             │
│   )                                                               │
├──────────────────────────────────────────────────────────────────┤
│ Step 3: detect_plagiarism 工具被 LLM 调用                          │
│                                                                   │
│ 工具契约:                                                          │
│   input_fields = (                                                │
│     InputField("document", "core.plain_text", "string"),         │
│     InputField("references", "core.text_collection", "list_string")│
│   )                                                               │
│                                                                   │
│ LLM 传入 explicit_kwargs = {}（未提供任何参数）                     │
│                                                                   │
│ ContractBinder.bind_tool_inputs() 自动解析：                       │
│                                                                   │
│ ┌─ document 字段 ──────────────────────────────────────┐         │
│ │ 源:  $ref:parse:latest (docaudit.parsed_document)    │         │
│ │ BFS: parsed_document → paragraph_list → plain_text   │         │
│ │ 投影链:                                                │         │
│ │   Step 1: parsed_document.to_paragraph_list (q=0.95) │         │
│ │     → 提取段落，创建 projected artifact               │         │
│ │   Step 2: paragraph_list.to_plain_text (q=0.98)      │         │
│ │     → 拼接段落，创建 projected artifact               │         │
│ │ 累积质量: 0.95 × 0.98 = 0.931                         │         │
│ │ 物化: materialize_as="string" → data["text"]         │         │
│ │ 结果: "第一章 总则\n第一条 ..."（纯文本字符串）        │         │
│ └──────────────────────────────────────────────────────┘         │
│                                                                   │
│ ┌─ references 字段 ────────────────────────────────────┐         │
│ │ 源:  $ref:search:latest (docaudit.search_results)    │         │
│ │ BFS: search_results → reference_text_list             │         │
│ │                    → text_collection                  │         │
│ │ 投影链:                                                │         │
│ │   Step 1: search_results.to_reference_text_list (0.92)│         │
│ │     → 过滤去重搜索结果                                │         │
│ │   Step 2: reference_text_list.to_text_collection (0.97)│        │
│ │     → 转为通用文本集合格式                             │         │
│ │ 物化: materialize_as="list_string"                    │         │
│ │ 结果: ["参考文本1...", "参考文本2...", ...]           │         │
│ └──────────────────────────────────────────────────────┘         │
│                                                                   │
│ 绑定完成后 kwargs = {                                             │
│   "document": "第一章 总则\n第一条 ...",                          │
│   "references": ["参考文本1...", "参考文本2...", ...]             │
│ }                                                                 │
│                                                                   │
│ → 执行抄袭检测，获得结果                                           │
│ → ArtifactStore.put(                                              │
│     Artifact(                                                     │
│       artifact_id="$ref:detect_plagiarism:latest",               │
│       artifact_type="docaudit.plagiarism_report",                │
│       data={matches: [{...}], overall_similarity: 0.15},         │
│       metadata=ArtifactMetadata(                                  │
│         created_by="detect_plagiarism",                           │
│         quality=1.0                                               │
│       )                                                           │
│     )                                                             │
│   )                                                               │
├──────────────────────────────────────────────────────────────────┤
│ Step 4: Agent Loop 监控                                           │
│                                                                   │
│ check_business_artifact_progress():                               │
│   → 检测到新的非 debug 工件 "docaudit.plagiarism_report"         │
│   → 重置无进展计数器                                              │
│                                                                   │
│ check_and_inject_hints():                                         │
│   → 扫描工具契约，发现 generate_report 的 input_fields 已可满足   │
│   → 注入提示: "以下终端工具已就绪，可考虑调用: generate_report"    │
└──────────────────────────────────────────────────────────────────┘
```

---

## 9. 设计亮点总结

### 9.1 声明式契约，松耦合

工具通过 `InputField` 声明需求，而非硬编码依赖特定工具的输出。两个工具可以独立开发，只要类型匹配，系统自动桥接。

```python
# 工具 A 只声明"我需要 plain_text"——不关心谁产生的
InputField(name="document", artifact_type="core.plain_text", materialize_as="string")

# 工具 B 只声明"我产出 parsed_document"
output_artifact_type = "docaudit.parsed_document"

# 系统自动通过投影器图找到 A ← B 的转换路径
```

### 9.2 图搜索自动路由

BFS 在投影器图中搜索最短/最优类型转换路径，无需手动指定转换链。随着投影器数量的增长，系统的连接能力呈指数级增长。

### 9.3 不可变数据

所有模型（`Artifact`, `ProjectionPlan`, `MaterializedBinding`）都使用 `frozen=True`（Pydantic）或 `frozen=True`（dataclass），确保：

- 数据流可审计追踪（完整 lineage）
- 无隐藏副作用
- 安全的并发访问

### 9.4 三层权限模型

```
ArtifactStore (主存储，无限制)
  └── ScopedArtifactStore (子代理包装，白名单过滤)
        └── ArtifactPermission (逐工件: project / materialize / debug_read)
```

子代理只能看到被显式授权的工件，但始终可以创建新工件——在隔离性和生产力之间取得平衡。

### 9.5 完整的可观测性

- `ProjectionTrace` — 记录每一步投影操作的输入/输出/缓存命中
- `ProjectionEvent` — 结构化事件流（resolved, projected, materialized, cached, failed）
- `artifact_bindings` — 返回具体哪个工件被绑定到了哪个参数

### 9.6 质量衰减感知

- 每个投影器声明 `quality_score`
- 多步投影链的质量 = 各步分数的累积
- `ProjectionPolicy.min_quality` 阈值自动过滤低质量路径
- 结合 `lossiness` 标记，让策略层决定是否接受有损转换

### 9.7 渐进式部署

`ProjectionFeatureFlags` 允许按特性开关控制投影行为，支持灰度发布：

- 新增投影器标记为 `experimental` → LLM 不可见，但 BFS 可用
- 验证稳定后 `promote(projector, "core")` → 全量可用
- 过期投影器标记 `deprecated` → 仅在 `allow_deprecated=True` 时可用
- 出问题的投影器标记 `disabled` → 完全从图中移除

### 9.8 文件组织规范

- 高内聚低耦合：每个文件职责单一
- models.py（数据模型）→ store.py（存储）→ resolver.py（搜索）→ executor.py（执行）→ projectors.py（投影器）→ binder.py（协调）
- 符合 Python 编码规范的 frozen dataclass + Pydantic BaseModel 不可变设计
- 完整测试覆盖（`tests/agent/artifacts/` 下有 5 个测试文件，外加工具级和集成测试）

---

## 相关文件索引

| 类别 | 文件 | 说明 |
|------|------|------|
| **核心模块** | `src/agent/artifacts/models.py` | 所有数据模型和 Schema 注册 |
| | `src/agent/artifacts/store.py` | ArtifactStore 和 ScopedArtifactStore |
| | `src/agent/artifacts/resolver.py` | BFS 投影路径搜索 |
| | `src/agent/artifacts/executor.py` | 投影执行和物化 |
| | `src/agent/artifacts/projectors.py` | 投影器和注册表 |
| | `src/agent/artifacts/binder.py` | ToolRegistry 协调器 |
| | `src/agent/artifacts/registry.py` | Session → Store 映射 |
| **集成点** | `src/agent/tools/registry.py` | 工具执行中的契约绑定 |
| | `src/agent/tools/builtin/get_artifact.py` | get_artifact 工具 |
| | `src/agent/tools/builtin/list_artifacts.py` | list_artifacts 工具 |
| | `src/agent/core/loop.py` | 主循环中的工件监控 |
| | `src/agent/core/loop_guards.py` | 进度追踪和探索循环检测 |
| | `src/agent/core/loop_hints.py` | 终端工具就绪提示 |
| | `src/agent/runtime/runtime.py` | 子代理 ScopedStore 创建 |
| | `src/agent/agents/base.py` | Agent 基类中的 Artifact 集成 |
| | `src/agent/agents/orch.py` | Orchestrator 中的 Artifact 工具 |
| | `src/agent/api/app.py` | SessionArtifactStoreRegistry 初始化 |
| | `src/plugin/manager.py` | Plugin 主机服务方法 |
| **测试** | `tests/agent/artifacts/test_models.py` | 模型测试 |
| | `tests/agent/artifacts/test_store.py` | Store 测试 |
| | `tests/agent/artifacts/test_resolver.py` | Resolver 测试 |
| | `tests/agent/artifacts/test_executor.py` | Executor 测试 |
| | `tests/agent/artifacts/test_projectors.py` | Projector 测试 |
| | `tests/agent/tools/test_get_artifact.py` | get_artifact 工具测试 |
| | `tests/agent/tools/test_list_artifacts.py` | list_artifacts 工具测试 |
