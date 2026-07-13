# CacheStore 统一缓存层 + 工具参数 ref_id 自动解析 设计

## 动机

当前 `read_cached_output` 的 query 结果直接返回给 LLM，不会被持久化。子代理每次需要同样的构造参数时都要重新执行 jq 查询。此外，非 JSON 的纯文本输出（如 shell stdout）也无法被持久化和引用。

改造目标：

1. 统一持久化层，支持 JSON 和纯文本两种格式
2. `read_cached_output` 的 query 结果自动被缓存，生成可引用的 ref_id
3. 工具调用参数中传入 ref_id 时，自动加载并填入（类型自适应）
4. ref_id 支持可读的语义标签

## 架构与组件

新建 `CacheStore` 类，作为缓存存储的**唯一入口**，从 `ContextManager` 中提取持久化逻辑。

```
                    ┌──────────────────┐
                    │  ToolRegistry     │
                    │  .execute()       │
                    │  执行后持久化结果   │
                    └────────┬─────────┘
                             │
                    ┌────────▼─────────┐
                    │   CacheStore      │  ← 新文件 cache_store.py
                    │  ───────────────  │
                    │  persist(data)     │
                    │  resolve(ref_id)   │
                    │  load(ref_id)      │
                    │  ref_map           │
                    │  ref_counters      │
                    └────────┬─────────┘
                             │
              ┌──────────────┼──────────────┐
              ▼              ▼              ▼
         .json 文件     .txt 文件     .schema.json
```

### 文件变更

| 文件 | 变更 |
|------|------|
| `src/agent/core/cache_store.py` | **新建** — 统一缓存存储层 |
| `src/agent/core/context_manager.py` | 重构 — persist/ref 逻辑委托给 CacheStore |
| `src/agent/tools/registry.py` | `execute()` 增加结果持久化 + 类型自适应解析 |
| `src/agent/tools/protocol.py` | `ToolProtocol` 增加 `skip_persist` 和 `output_content_type` 可选属性 |
| `tests/agent/test_cache_store.py` | **新建** — CacheStore 单元测试 |
| `tests/agent/test_context_manager.py` | 更新 — 适配重构后的接口 |

### 职责划分

- **CacheStore**：数据持久化（JSON/文本）、ref_id 生成与管理、文件读写、schema 提取、ref 解析与加载
- **ContextManager**：三层上下文预算控制（Layer1/2/3），委托 CacheStore 处理持久化细节
- **ToolRegistry**：工具发现与执行，执行后调用 CacheStore 统一持久化结果，解析前调用 CacheStore 解析 ref 参数

## 数据流

### 工具执行 → 统一持久化

```
Tool.execute() 返回 ToolResult
  → ToolRegistry.execute() 获取 result.data
  → CacheStore.persist(data, tool_name)
      │
      ├─ data 是 None → 跳过
      ├─ data 是 dict/list → json.dumps → 存 .json + 可选 .schema.json
      ├─ data 是 str（非 JSON）→ 存 .txt
      └─ data 是其他类型 → str(data) → 存 .txt
      │
      ├─ 序列化大小 ≤ 阈值(3KB) → 返回原始 data（小数据不包装）
      └─ 序列化大小 > 阈值 → 返回 __persisted_output__ 标记
                                  {ref_id, file, size_chars, preview, content_type, label, source}
  → 最终返回 ToolResult(data=原始或标记)
```

### 子代理使用 read_cached_output 构造参数

```
read_cached_output(ref_id="$ref:parse_doc:1", query="{id: .metadata.id, text: .pages[].content}")
  → read_cached_output 内部执行 jq → 返回 ToolResult(data={id: "D001", text: "..."})
  → ToolRegistry.execute() 外层调用 CacheStore.persist({id: "D001", text: "..."}, "read_cached_output")
  → 若数据大，返回 __persisted_output__ 标记，ref_id="$ref:read_cached_output:2"
```

### 下游工具调用时 ref 解析（类型自适应）

```
some_tool(params="$ref:read_cached_output:2")
  → ToolRegistry.execute() 调用 CacheStore.resolve_refs(kwargs, tool.parameters)
      │
      ├─ 识别 "$ref:read_cached_output:2"
      ├─ 加载文件 → 检测内容类型
      ├─ 查看 some_tool.parameters.properties.params 的 type
      │   ├─ type: "string" → 返回原始文本
      │   ├─ type: "object" → 尝试 json.loads
      │   ├─ type: "array" → 尝试 json.loads，需为数组类型
      │   └─ type: "number"/"integer"/"boolean" → 尝试转换
      └─ 替换 kwargs["params"] = 解析后的值
  → some_tool.execute(**resolved_kwargs)
```

### 纯文本场景

```
ShellTool 执行 → ToolResult(data="command output text\nline2\n...")
  → CacheStore.persist("command output text\nline2\n...", "run_shell")
  → 检测到是纯文本字符串 → 存 .txt，content_type="text/plain"
  → 超过阈值 → 返回 __persisted_output__ 标记，ref_id="$ref:run_shell:3"

audit_report(content="$ref:run_shell:3")
  → 参数 content 的 type 是 "string"
  → CacheStore.resolve_refs → 加载 .txt → 直接返回文本
```

## CacheStore API

```python
class CacheStore:
    """统一缓存存储层，支持 JSON 和纯文本格式。"""

    def __init__(self, cache_dir: str = ".agent_cache") -> None: ...

    # -- 持久化 --
    def persist(
        self,
        data: Any,
        tool_name: str,
        *,
        label: str | None = None,
        source_ref_id: str | None = None,
        source_query: str | None = None,
    ) -> PersistResult:
        """
        持久化数据，根据内容类型自动选择 JSON 或文本格式。

        Returns:
            PersistResult:
              - data: 原始数据（小数据）或 __persisted_output__ 标记（大数据）
              - ref_id: 生成的引用 ID
              - persisted: 是否实际持久化了（超过阈值才为 True）
        """
        ...

    # -- 解析 --
    def resolve_refs(
        self,
        kwargs: dict[str, Any],
        param_schemas: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """递归解析 kwargs 中的 $ref 引用，根据参数类型自适应。"""
        ...

    def load(self, ref_id: str) -> Any:
        """加载 ref_id 对应的原始数据。"""
        ...

    # -- 查询 --
    def exists(self, ref_id: str) -> bool: ...
    def get_info(self, ref_id: str) -> dict | None: ...

    # -- 状态 --
    ref_map: dict[str, str]
    ref_counters: dict[str, int]
```

### PersistResult

```python
@dataclass
class PersistResult:
    data: Any          # 返回给 LLM 的数据（原始或标记）
    ref_id: str        # 生成的引用 ID
    persisted: bool    # 是否实际写入磁盘
```

### ref_id 格式

`$ref:<tool_name>:<seq>[:<label>]`

- `$ref:parse_document:1`
- `$ref:read_cached_output:2:doc_params`
- `$ref:run_shell:3`

### `__persisted_output__` 标记结构

```python
{
    "__persisted_output__": True,
    "ref_id": "$ref:read_cached_output:2:doc_params",
    "file": ".agent_cache/read_cached_output_2_1716xxx.json",
    "size_chars": 5000,
    "preview": "...前200字符...",
    "content_type": "application/json",
    "label": "doc_params",                     # 如有
    "source": {                                # 如有
        "ref_id": "$ref:parse_document:1",
        "query": "{id: .metadata.id}",
    },
}
```

### 内容类型检测

```python
def _detect_content_type(self, data: Any) -> tuple[str, str]:
    """
    Returns (content_type, serialized_string)
    - dict/list → "application/json", json.dumps(data)
    - str (valid json) → "application/json", data
    - str (non-json) → "text/plain", data
    - other → "text/plain", str(data)
    """
```

### 缓存文件布局

```
.agent_cache/
├── parse_document_1_1779860476616.json
├── parse_document_1_1779860476616.schema.json
├── read_cached_output_2_1779860477000.json
├── run_shell_3_1779860478000.txt
```

## ToolProtocol 扩展

```python
@runtime_checkable
class ToolProtocol(Protocol):
    name: str
    description: str
    parameters: dict
    output_schema: dict | None           # 已有
    skip_persist: bool                   # 新增，默认 False
    output_content_type: str | None      # 新增，默认 None（自动检测）

    async def execute(self, **kwargs: Any) -> ToolResult: ...
```

Registry 层检测 `__persisted_output__` 标记，避免二次包装（`$ref:$ref:...` 嵌套）。

## 错误处理

| 场景 | 行为 |
|------|------|
| ref_id 格式无效 | `load()` 返回原始字符串，`resolve_refs()` 保留原值 + warn |
| ref_id 不在 ref_map | `load()` 返回原始字符串 |
| 缓存文件不存在 | `load()` 返回原始 ref_id 字符串 + warn |
| JSON 解析失败 | `resolve_refs()` 返回原始文本 + warn |
| 纯文本文件，参数期望 object | `resolve_refs()` 返回原始文本 + warn |
| 内容类型无法识别 | 默认当作文本，存 `.txt` |
| 磁盘写入失败 | 返回原始 data（不包装），warn 日志 |
| Schema 提取异常 | 静默跳过，不影响数据持久化（best-effort） |
| 递归解析深度 > 32 | 停止解析，保留原值 + warn |

核心原则：持久化和解析是**尽力而为**的辅助功能，任何时候都不应阻断正常的工具调用流程。

## 测试策略

| 测试类别 | 覆盖点 |
|---------|--------|
| **CacheStore 单元** | JSON/文本持久化、小数据不包装、label 生成 ref_id、source 元数据、内容类型检测、ref 解析（各类型自适应）、文件不存在降级、磁盘满降级 |
| **ContextManager 集成** | 重构后 persist_large_output / micro_compact / resolve_refs 行为不变 |
| **ToolRegistry 集成** | execute 后自动持久化、skip_persist 工具不持久化、类型自适应解析 |
| **read_cached_output 端到端** | persist → read schema → jq query → query 结果被持久化 → 下游工具通过 ref_id 引用 → 解析为正确类型 |
| **纯文本端到端** | run_shell 输出文本 → 持久化 .txt → 下游工具通过 $ref 引用 → string 类型参数直接获取文本 |
