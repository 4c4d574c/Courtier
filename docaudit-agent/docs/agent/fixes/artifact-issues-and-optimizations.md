# Artifact 系统问题分析与优化建议

> **基于代码版本:** 2026-07-04
> **分析范围:** `src/agent/artifacts/`、`src/agent/tools/registry.py`、`src/agent/core/loop_hints.py`、`src/agent/runtime/runtime.py`、`src/plugin/proxies.py`
> **前置阅读:** [artifact-system-guide.md](./artifact-system-guide.md)、[artifact-integration-guide.md](./artifact-integration-guide.md)
> **状态:** ✅ 所有 13 个问题已修复（P1.3 为误报，无需修复）

---

## 目录

1. [问题分级总览](#1-问题分级总览)
2. [P0 — 会导致运行时错误](#2-p0--会导致运行时错误)
3. [P1 — 架构设计问题](#3-p1--架构设计问题)
4. [P2 — 逻辑缺陷与浪费](#4-p2--逻辑缺陷与浪费)
5. [P3 — 代码质量与健壮性](#5-p3--代码质量与健壮性)
6. [优化路线图](#6-优化路线图)

---

## 1. 问题分级总览

| 级别 | 数量 | 影响 | 状态 |
|------|------|------|------|
| P0 — 会导致运行时错误 | 1 | `materialize_as` 默认值 bug | ✅ 已修复 |
| P1 — 架构设计问题 | 3 | 自定义扩展无法生效、语义错误、数据不一致 | ✅ 已修复（1 误报） |
| P2 — 逻辑缺陷与浪费 | 4 | 性能浪费、权限形同虚设、数据丢失 | ✅ 已修复 |
| P3 — 代码质量 | 5 | 可维护性、健壮性 | ✅ 已修复 |

---

## 2. P0 — 会导致运行时错误

### 2.1 `build_contract_from_input_fields` 的 materialize_as 默认值错误

**位置:** `src/agent/artifacts/models.py:593-610`

**问题代码：**

```python
def build_contract_from_input_fields(
    tool_name: str,
    input_fields: tuple[InputField, ...],
) -> tuple[InputField, ...]:
    return tuple(
        InputField(
            name=f.name,
            artifact_type=f.artifact_type,
            materialize_as=f.materialize_as or f.artifact_type,  # ← BUG
            ...
        )
        for f in input_fields
    )
```

**问题：** 当 `InputField` 的 `materialize_as` 为 `None`（默认值）时，`f.materialize_as or f.artifact_type` 会将物化形式设置为类型名字符串本身，例如 `"core.plain_text"` 而不是 `"string"`。这导致 `MaterializerRegistry.materialize()` 使用 key `("core.plain_text", "core.plain_text")` 查找物化器，而注册表中只有 `("core.plain_text", "string")`。

**当前为何没有触发：**
- `core.plain_text` 在 `_PATH_DEFAULTS`（`resolver.py:28-30`）中有 `"$.text"` 路径，`materialize()` 的路径回退逻辑（`executor.py:70-73`）救了它
- 所有现有工具的 `InputField` 都显式指定了 `materialize_as`，None 路径从未被执行
- 但任何一个新工具如果省略 `materialize_as`，且目标类型不在 `_PATH_DEFAULTS` 中，就会触发 `KeyError`

**修复建议：**

```python
# models.py:605 — 替换默认值逻辑
materialize_as=f.materialize_as or "dict",  # 安全兜底：整个 data 作为 dict
```

或者更精确的方案——根据 artifact_type 推断合理的 materialize_as：

```python
_MATERIALIZE_AS_DEFAULTS: dict[str, str] = {
    "core.plain_text": "string",
    "core.text_collection": "list_string",
    "core.json_object": "dict",
    "core.error_report": "dict",
    "core.debug_view": "dict",
    "docaudit.parsed_document": "dict",
    "docaudit.paragraph_list": "list_dict",
    "docaudit.search_results": "dict",
    "docaudit.document_metadata": "dict",
    "docaudit.document_structure": "dict",
    "docaudit.audit_finding_list": "list_dict",
    "docaudit.audit_report": "dict",
    "docaudit.reference_text_list": "list_dict",
    "docaudit.plagiarism_report": "dict",
}

def build_contract_from_input_fields(...):
    return tuple(
        InputField(
            ...
            materialize_as=f.materialize_as or _MATERIALIZE_AS_DEFAULTS.get(f.artifact_type, "dict"),
            ...
        )
        ...
    )
```

---

## 3. P1 — 架构设计问题

### 3.1 ProjectorRegistry 每次调用都重新创建，自定义投影器无法全局生效

**影响范围：** 任何通过场景 E 新增的自定义投影器都无法在生产环境中生效。

**根因：** 系统中有 **6 个位置**各自调用 `create_default_projector_registry()` 创建独立实例：

| 位置 | 文件 | 行号 |
|------|------|------|
| ContractBinder (resolver) | `src/agent/artifacts/binder.py` | 56 |
| ContractBinder (executor) | `src/agent/artifacts/binder.py` | 75 |
| ToolRegistry init | `src/agent/tools/registry.py` | 39 |
| GetArtifactTool | `src/agent/tools/builtin/get_artifact.py` | 123 |
| ListArtifactsTool | `src/agent/tools/builtin/list_artifacts.py` | 98 |
| Loop hints | `src/agent/core/loop_hints.py` | 53 |

每个位置创建的是**独立的、完全相同的** Registry 副本。即使在某处注册了自定义投影器，其他调用点完全看不到。更糟糕的是，`ContractBinder` 内部的两个调用（resolver 和 executor）使用不同的实例——虽然不是 bug（它们不互相修改 registry），但这意味着每两次投影操作就创建两个 Registry 对象。

MaterializerRegistry 也有类似问题（`binder.py:76` 和 `get_artifact.py:174` 各自调用 `MaterializerRegistry.default()`）。

**修复建议：**

将 ProjectorRegistry 和 MaterializerRegistry 提升为应用级单例，在 `app.py` 中创建并通过依赖注入传递给所有消费者：

```python
# 方案：在 app.py 中创建全局实例
app.state.projector_registry = create_default_projector_registry()
app.state.materializer_registry = MaterializerRegistry.default()

# 注册自定义投影器
app.state.projector_registry.register(my_custom_projector)

# 传递给 ContractBinder、ToolRegistry 等
ContractBinder(artifact_store, projector_registry=app.state.projector_registry, ...)
```

`ToolRegistry.__init__()` 已有 `projector_registry` 参数（`registry.py:30`），可直接注入。`ContractBinder` 需要新增该参数——当前 binder.py:56/75 硬编码调用 `create_default_projector_registry()`。

### 3.2 `_UPSTREAM_PRODUCERS` 硬编码映射语义错误

**位置:** `src/agent/artifacts/models.py:523-529`

**问题代码：**

```python
_UPSTREAM_PRODUCERS: dict[str, str] = {
    "docaudit.parsed_document": "parse_document",
    "docaudit.search_results": "search_documents",
    "core.plain_text": "parse_document",        # ← 错误
    "core.text_collection": "search_documents",  # ← 错误
    "docaudit.reference_text_list": "search_documents",
}
```

**问题：**
- `core.plain_text` 被映射到 `parse_document`，但 `parse_document` 的 `output_artifact_type` 是 `docaudit.parsed_document`，不是 `core.plain_text`。`plain_text` 是通过投影间接产生的
- 同样 `search_documents` 产出 `docaudit.search_results`，`text_collection` 也需要投影
- 当 `loop_hints.py` 使用这个映射生成"建议调用 X 工具"的提示时，会建议调用 `parse_document` 来获取 `core.plain_text`——`parse_document` 确实能间接产生 `plain_text`（通过投影），但语义上不精确
- 更重要的是：这个静态映射与 `derive_upstream_producers()` 动态推导的结果可能不一致，且永远不会同步更新

**修复建议：**

删除 `_UPSTREAM_PRODUCERS` 静态映射，统一使用 `derive_upstream_producers()`（`models.py:613`）动态推导。`derive_upstream_producers()` 已正确处理直接生产者和间接生产者（通过投影图可达性）：

```python
# 当前 resolver.py:67 已经接收动态 producers 参数
upstream = producers.get(field.artifact_type, []) if producers else []

# 只需确保所有调用点都传入 producers
# loop_hints.py:53 当前未传入 producers，需要修复
```

### 3.3 `_search_results_to_reference_text_list` 字段名与 Schema 定义不一致

**位置:** `src/agent/artifacts/projectors.py:375`

**问题代码：**

```python
# projectors.py:375 — 投影函数取 chunk_text
text = str(hit.get("chunk_text", "")).strip()
```

但在 `models.py` 中 `docaudit.search_results` 的 Schema 定义（约 line 265-295）：

```python
"hits": {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "text": {"type": "string"},       # ← Schema 中是 "text"
            "score": {"type": "number"},
            "source": {"type": "string"},
        }
    }
}
```

Schema 中定义的是 `"text"`，投影函数取的却是 `"chunk_text"`。如果 search 工具产出的数据使用 `"text"` 字段（符合 Schema），那么投影函数将永远提取不到文本（`chunk_text` 不存在 → `""` → 被 `len(text) < min_text_chars` 过滤），导致搜索结果无法投影到 reference_text_list。

**修复建议：**

```python
# projectors.py:375
text = str(hit.get("text", "")).strip()  # 与 Schema 对齐
```

同时检查 search 工具的实际输出格式，确保生产者和消费者使用相同的字段名。

---

## 4. P2 — 逻辑缺陷与浪费

### 4.1 子代理权限隔离形同虚设

**位置:** `src/agent/runtime/runtime.py:429-448`

**问题代码：**

```python
def _build_scoped_store(self, artifact_store: Any) -> Any:
    if not isinstance(artifact_store, ArtifactStore):
        return artifact_store
    entries: list[ArtifactContextEntry] = []
    for artifact in artifact_store.list_all():
        if artifact.metadata.debug_only:
            continue
        entries.append(
            ArtifactContextEntry(
                ref=artifact.artifact_id,
                permissions=ArtifactPermission(
                    project=True,       # ← 所有工件都授予完全权限
                    materialize=True,   # ← 所有工件都授予完全权限
                    debug_read=False,
                ),
            )
        )
    return ScopedArtifactStore(...)
```

**问题：** `ScopedArtifactStore` 和 `ArtifactPermission` 的设计意图是细粒度权限控制——部分工件可投影但不可物化，部分反之。但 `_build_scoped_store` 给**所有非 debug 工件**统一授予 `project=True, materialize=True`，使得权限系统完全失去区分度。子代理实际可以访问主 Store 中的所有业务工件。

**为什么可能是故意的：** 在 DocAudit 的单个审计会话中，所有工件通常属于同一文档，不需要隔离。但如果未来引入多文档同时审计或跨会话工件共享，这个设计就会成为安全隐患。

**修复建议：**
- 短期：添加注释说明这是有意为之的简化设计，并说明适用边界
- 长期：根据工件的 `semantic_role`、`sensitivity` 或 `created_by` 实现差异化权限，例如只允许子代理访问与其任务相关的工件

### 4.2 `_register_output_artifact_simple` 使用固定 `$ref:tool:latest`，多次调用会覆盖

**位置:** `src/agent/tools/registry.py:324-349`

**问题代码：**

```python
def _register_output_artifact_simple(self, *, tool_name, artifact_type, result, artifact_store):
    ...
    ref_id = f"$ref:{tool_name}:latest"   # ← 固定 ID
    artifact_store.register_cached_ref(
        ref_id=ref_id, ...
    )
```

**问题：** 同一个工具在一个会话中被多次调用时（例如 `search` 被调用 3 次），每次都用 `$ref:search:latest`，后面的调用直接**覆盖**前面的结果。旧的搜索结果工件从 Store 中消失。如果下游工具已经引用了 `$ref:search:latest`，实际会拿到最后一次调用结果而非当初绑定的那次。

**当前影响范围：** `contract_binder` 使用 `artifact_bindings` 记录具体绑定了哪个工件，如果绑定的是 `$ref:search:latest` 但在绑定后又发生了新的 search 调用，那么物化时取到的是新数据而非原来的。不过由于 Agent Loop 是顺序执行的，绑定和使用之间通常不会有新的同工具调用，所以实践中可能很少触发。

**修复建议：**

使用自增序号替代固定 latest：

```python
# registry.py
_ref_counters: dict[str, int] = {}  # tool_name → counter

def _register_output_artifact_simple(self, ...):
    count = self._ref_counters.get(tool_name, 0) + 1
    self._ref_counters[tool_name] = count
    ref_id = f"$ref:{tool_name}:{count}"
    ...
```

### 4.3 loop_hints.py 中未传入 producers，导致 suggested_actions 为空

**位置:** `src/agent/core/loop_hints.py:53`

**问题代码：**

```python
# loop_hints.py:53
resolver = ProjectionResolver(create_default_projector_registry())
resolution = resolver.resolve(fields, tool.name, candidates, ProjectionPolicy())
#                                                          ^^^^^^^^^^^^^^^^
#                                                          producers 参数未传入
```

而在 `ToolRegistry._bind_contract_arguments` 中（`registry.py:310-322`）：

```python
producers = self._get_producers()  # 动态推导的生产者映射
binding_result = self._bind_contract_arguments(
    fields=effective_fields,
    tool_name=name,
    artifact_store=artifact_store,
    explicit_kwargs=kwargs,
    producers=producers,  # ← 传入了 producers
)
```

**问题：** `loop_hints.py` 在生成"建议调用 X 工具"的提示时，由于没有传入 `producers`，`resolver.resolve()` 在字段解析失败时无法生成 `suggested_actions`（`resolver.py:67-78` 中 `producers.get(field.artifact_type, [])` 返回空列表）。结果是：终端工具就绪检测不受影响，但"被阻塞的工具缺少什么"的提示中不会包含"建议先调用某某工具"的信息。

**修复建议：**

```python
# loop_hints.py — 复用 tool_registry 的 producers 缓存
from ..tools.registry import ToolRegistry

def _iter_terminal_resolutions(tool_registry, artifact_store, ...):
    producers = None
    if isinstance(tool_registry, ToolRegistry):
        producers = tool_registry._get_producers()  # 或暴露为公开属性
    ...
    resolution = resolver.resolve(fields, tool.name, candidates, ProjectionPolicy(),
                                  producers=producers)
```

### 4.4 ContractBinder 内 resolver 和 executor 使用不同的 ProjectorRegistry 实例

**位置:** `src/agent/artifacts/binder.py:56,75`

```python
# binder.py:56 — 用于 resolver
resolver = ProjectionResolver(create_default_projector_registry())

# binder.py:75 — 用于 executor
executor = ProjectionExecutor(
    projector_registry=create_default_projector_registry(),  # ← 又一个新实例
    ...
)
```

**问题：** 虽然两次调用返回的 Registry 内容相同（都是默认投影器），但如果未来支持运行时动态注册投影器（例如 Plugin 注册自己的投影器），resolver 搜索时看到的图和 executor 执行时看到的图可能不同。BFS 找到的路径中的投影器在执行时可能不存在。

**修复建议：** 创建一次，复用：

```python
registry = create_default_projector_registry()
resolver = ProjectionResolver(registry)
executor = ProjectionExecutor(projector_registry=registry, ...)
```

---

## 5. P3 — 代码质量与健壮性

### 5.1 `_parsed_document_to_paragraph_list` 数据结构访问路径硬编码

**位置:** `src/agent/artifacts/projectors.py:294-335`

```python
for page_index, page in enumerate(artifact.data.get("pages", []) or []):
    body = ((page.get("page_content") or {}).get("body") or {})
    _append_paragraph(paragraphs, body.get("title"), ...)
    for paragraph_index, paragraph in enumerate(body.get("main_text", []) or []):
        ...
```

**问题：** 嵌套路径 `page → page_content → body → title/main_text` 硬编码在投影函数中。如果 parse 工具的 Document 模型结构发生变化（例如 `page_content` 重命名为 `content`），投影函数会静默产出空段落列表（`confidence=0.0`），而不是报错。

**建议：** 在投影函数开头增加结构断言或至少记录 warning 日志：

```python
if not paragraphs:
    logger.warning(
        "No paragraphs extracted. Expected structure: pages[].page_content.body.{title,main_text}. "
        "Got keys: %s",
        [list(p.keys()) for p in artifact.data.get("pages", [])[:1]],
    )
```

### 5.2 BFS 队列元素冗余

**位置:** `src/agent/artifacts/resolver.py:134-135`

```python
queue: deque[tuple[Artifact, tuple[ProjectionStep, ...], str, str, float]] = deque(
    [(source_artifact, (), source_artifact.artifact_type, source_artifact.schema_version, 1.0)]
)
```

**问题：** 队列元素是一个 5 元组，类型标注长达 80+ 字符。`source_artifact` 在整个 BFS 过程中不变，但被放入每个队列元素。虽然 Python 传递的是引用而非拷贝（无性能问题），但可读性很差。

**建议：** 将 `source_artifact` 从队列元素中提取出来，只在最终构建 `ProjectionPlan` 时使用：

```python
# 提取 source_artifact 到循环外部
queue: deque[tuple[tuple[ProjectionStep, ...], str, str, float]] = deque(
    [((), source_artifact.artifact_type, source_artifact.schema_version, 1.0)]
)
```

### 5.3 BFS 双重深度检查

**位置:** `src/agent/artifacts/resolver.py:140,164`

```python
while queue:
    source, steps, current_type, current_version, current_score = queue.popleft()
    if len(steps) > policy.max_depth:     # ← 第1次检查
        continue
    if current_type == field.artifact_type:
        ...  # 找到目标
        continue
    ...
    if len(steps) >= policy.max_depth:    # ← 第2次检查
        continue
    for projector in self._registry.outgoing(current_type):
        ...
```

**问题：** 第 1 次检查（`>`）发生在出队后、匹配检测之前；第 2 次检查（`>=`）发生在展开子节点之前。逻辑上可以合并为：
- 匹配检测在 `len(steps) <= policy.max_depth` 时进行（零步投影 depth=0 总能匹配）
- 展开在 `len(steps) < policy.max_depth` 时进行

这不是 bug，但代码意图不够清晰，两个 `continue` 位置分散。

### 5.4 `ProjectionEvent` 使用 `time.time()` 而非 `time.monotonic()`

**位置:** `src/agent/artifacts/resolver.py:263`

```python
def emit_event(event: str, details: dict | None = None) -> ProjectionEvent:
    evt = ProjectionEvent(
        event=event,
        timestamp=time.time(),  # ← 受系统时钟调整影响
        details=details or {},
    )
```

**问题：** `time.time()` 可能因 NTP 同步、夏令时或手动调整而跳变。在分布式追踪场景中，这会导致事件时间线混乱。`ProjectionTrace` 用于可观测性，时间戳准确性很重要。

**建议：** `time.monotonic()` 用于计算时间间隔，`time.time()` 保留用于绝对时间戳（与其他系统对齐）。如果只用于日志排序和持续时间计算，用 `time.monotonic()` 更安全。

### 5.5 `register_cached_ref` 的 schema_version 硬编码

**位置:** `src/agent/artifacts/store.py:100`

```python
artifact = Artifact(
    artifact_id=ref_id,
    artifact_type=artifact_type,
    schema_version="1.0",   # ← 硬编码
    data=data,
    metadata=metadata,
)
```

**问题：** 所有通过 `register_cached_ref` 创建的工件都被标记为 schema_version `"1.0"`，无论实际类型定义的版本是什么。`check_schema_version_compatible()` 在 BFS 中检查 `projector.spec.source_schema_version` 约束时，会用到这个值。如果某个类型升级到了 `"2.0"` 但 `register_cached_ref` 仍然写入 `"1.0"`，可能导致版本兼容性检查误判。

**建议：** 从 `get_artifact_schema(artifact_type)` 读取实际的 `schema_version`：

```python
schema = get_artifact_schema(artifact_type)
artifact = Artifact(
    ...
    schema_version=schema.schema_version if schema else "1.0",
    ...
)
```

---

## 6. 优化路线图

### 第一阶段：修复 Bug（1-2 天）

| 序号 | 问题 | 文件 | 改动量 |
|------|------|------|--------|
| 1 | `build_contract_from_input_fields` 默认值（P0） | `models.py:605` | 1 行 |
| 2 | `_search_results_to_reference_text_list` 字段名（P1） | `projectors.py:375` | 1 行 |
| 3 | `loop_hints.py` 传入 producers（P2） | `loop_hints.py:53` | ~5 行 |
| 4 | `register_cached_ref` 使用实际 schema_version（P3） | `store.py:100` | ~3 行 |
| 5 | `time.time()` → 区分使用场景（P3） | `resolver.py:263` | 1 行 |

### 第二阶段：架构改进（3-5 天）

| 序号 | 问题 | 改动范围 |
|------|------|----------|
| 6 | ProjectorRegistry / MaterializerRegistry 全局单例化（P1） | `app.py` + `binder.py` + `get_artifact.py` + `list_artifacts.py` + `loop_hints.py` |
| 7 | 删除 `_UPSTREAM_PRODUCERS`，统一使用 `derive_upstream_producers`（P1） | `models.py` + `loop_hints.py` |
| 8 | `$ref:tool:latest` → 自增序号避免覆盖（P2） | `registry.py` |
| 9 | ContractBinder 内复用同一个 Registry 实例（P2） | `binder.py:56,75` |

### 第三阶段：增强与优化（按需）

| 序号 | 问题 | 说明 |
|------|------|------|
| 10 | 子代理权限隔离实际化（P2） | `runtime.py:429-448` — 按 semantic_role/sensitivity 差异化授权 |
| 11 | BFS 重构：队列类型简化 + 深度检查合并（P3） | `resolver.py:134-164` — 不影响功能，改善可维护性 |
| 12 | `_parsed_document_to_paragraph_list` 添加结构诊断日志（P3） | `projectors.py:294-335` — 方便排查数据格式变更 |
| 13 | `resolve()` 支持 `max_results` 限制，避免 Store 中大量工件导致性能问题 | `resolver.py:105-123` — 当前对所有候选工件都做 BFS |

---

## 附录：受影响的文件清单

| 文件 | P0 | P1 | P2 | P3 |
|------|:--:|:--:|:--:|:--:|
| `src/agent/artifacts/models.py` | ✓ | ✓ | | |
| `src/agent/artifacts/projectors.py` | | ✓ | | ✓ |
| `src/agent/artifacts/resolver.py` | | | | ✓ |
| `src/agent/artifacts/binder.py` | | ✓ | ✓ | |
| `src/agent/artifacts/store.py` | | | | ✓ |
| `src/agent/artifacts/executor.py` | | | | |
| `src/agent/tools/registry.py` | | | ✓ | |
| `src/agent/tools/builtin/get_artifact.py` | | ✓ | | |
| `src/agent/tools/builtin/list_artifacts.py` | | ✓ | | |
| `src/agent/core/loop_hints.py` | | ✓ | ✓ | |
| `src/agent/runtime/runtime.py` | | | ✓ | |
| `src/agent/api/app.py` | | ✓ | | |
