# Artifact 系统对接指南

> **适用场景:** 创建新工具时，需要与 Artifact 系统对接（产出/消费类型化工件）
> **前置阅读:** [artifact-system-guide.md](./artifact-system-guide.md)
> **最后更新:** 2026-07-04

---

## 目录

1. [决策树：我应该改哪些文件？](#1-决策树我应该改哪些文件)
2. [场景 A：工具只产出工件](#2-场景-a工具只产出工件)
3. [场景 B：工具只消费工件（自动绑定参数）](#3-场景-b工具只消费工件自动绑定参数)
4. [场景 C：工具既消费又产出](#4-场景-c工具既消费又产出)
5. [场景 D：注册新的 ArtifactType](#5-场景-d注册新的-artifacttype)
6. [场景 E：注册新的 Projector（类型转换器）](#6-场景-e注册新的-projector类型转换器)
7. [场景 F：Plugin 工具对接](#7-场景-fplugin-工具对接)
8. [高级控制](#8-高级控制)
9. [对接检查清单](#9-对接检查清单)
10. [已有类型与投影器速查](#10-已有类型与投影器速查)

---

## 1. 决策树：我应该改哪些文件？

```
你要创建什么？
│
├── 内置工具（Python 进程内运行，不独立部署）
│   │
│   ├── 只需声明 artifact 属性（不改任何架构文件）
│   │   └── 新建 1 个文件: src/agent/tools/builtin/<my_tool>.py
│   │       修改 1 个文件: src/agent/tools/builtin/__init__.py（添加导出）
│   │       修改 1 个文件: src/agent/agents/orch.py（注册到 tools 列表）
│   │
│   └── 还需要新的 ArtifactType／新的 Projector
│       ├── 新建 ArtifactType Schema 注册模块（1 个新文件）
│       │   修改 1 个文件: src/agent/api/app.py（启动时调用注册）
│       └── 新建/修改 Projector 注册
│           修改 1 个文件: src/agent/artifacts/projectors.py
│                          （在 create_default_projector_registry() 中新增）
│
├── Plugin 工具（独立 TCP 服务，可独立部署升级）
│   │
│   └── 新建一个完整的 Plugin 目录:
│       plugins/{category}/{plugin_name}/
│       ├── plugin.yaml         （新建 — 声明工具名、依赖、权限）
│       ├── tools.py            （新建 — 工具类，声明 artifact 属性）
│       ├── entry.py            （新建 — 注册工具到 PluginRuntime）
│       ├── pyproject.toml      （新建 — Python 项目元信息）
│       └── .venv/              （uv sync 自动创建）
│
│       无需修改任何已有文件 — PluginScanner 自动发现
│
└── 只在已有工具上添加 artifact 声明（不创建新工具）
    └── 修改 1 个文件: 该工具所在的 .py 文件
       在类上添加 output_artifact_type 和/或 input_fields 属性即可
```

---

## 2. 场景 A：工具只产出工件

> **效果：** 工具执行成功后，`result.data` 自动包装为 `Artifact` 存入 `ArtifactStore`，后续工具可通过自动绑定消费。

### 2.1 做法：修改工具类所在的文件

**修改的文件：** 你的工具类所在的 `.py` 文件（只需加一行类属性）

```python
from src.agent.tools.protocol import ToolResult


class ParseTool:
    name = "parse_document"
    description = "解析 PDF/DOCX 文档，返回内部 Document 模型"
    parameters = { ... }

    # ★ 加这一行即可
    output_artifact_type: str | None = "docaudit.parsed_document"

    async def execute(self, **kwargs) -> ToolResult:
        file_path = kwargs["file_path"]
        parsed = do_parse(file_path)
        return ToolResult(
            success=True,
            data={
                "pages": parsed.pages,
                "metadata": parsed.metadata,
            },
        )
```

### 2.2 约束

`output_artifact_type` 的值必须引用一个已在 Schema 注册表中存在的类型名。当前已有类型见[第 10 节速查表](#101-已有-artifacttype)。如需自定义类型，见[场景 D](#5-场景-d注册新的-artifacttype)。

`result.data` 的结构应与该类型的 JSON Schema 一致。

### 2.3 原理

`ToolRegistry.execute()`（`src/agent/tools/registry.py:251-259`）检测到 `output_artifact_type` 不为 None 且执行成功时，调用 `_register_output_artifact_simple()`（同文件 line 324）：

```
result.data ──▶ Artifact(
                    artifact_id="$ref:{tool_name}:latest",
                    artifact_type=output_artifact_type,
                    data=result.data,
                )
             ──▶ ArtifactStore.put(artifact)
```

---

## 3. 场景 B：工具只消费工件（自动绑定参数）

> **效果：** 当 LLM 调用工具但未传入某个必填参数时，系统从 `ArtifactStore` 中自动查找匹配的工件，经过类型投影转换后注入到 `kwargs` 中。

### 3.1 做法：修改工具类所在的文件

**修改的文件：** 你的工具类所在的 `.py` 文件（添加 `input_fields` 类属性 + 添加 import）

```python
# ★ 新增 import
from src.agent.artifacts.models import InputField
from src.agent.tools.protocol import ToolResult


class GenerateReportTool:
    name = "generate_report"
    description = "汇总审计发现，生成最终报告"
    parameters = {
        "type": "object",
        "properties": {
            "findings": {"type": "array", "items": {"type": "object"}},
            "document_text": {"type": "string"},
        },
        "required": ["findings", "document_text"],
    }

    # ★ 加这个类属性
    input_fields: tuple[InputField, ...] = (
        InputField(
            name="findings",                    # ← 对应 execute(**kwargs) 的参数名
            artifact_type="docaudit.audit_finding_list",
            materialize_as="list_dict",
        ),
        InputField(
            name="document_text",
            artifact_type="core.plain_text",
            materialize_as="string",
        ),
    )

    async def execute(self, **kwargs) -> ToolResult:
        # kwargs["findings"] 和 kwargs["document_text"] 已被自动注入
        findings = kwargs["findings"]        # list[dict]
        doc_text = kwargs["document_text"]   # str
        ...
```

### 3.2 三条命名一致性要求

`InputField.name` 必须同时匹配三处：

| 位置 | 示例 |
|------|------|
| `InputField(name=...)` | `name="findings"` |
| `parameters["properties"]` 的 key | `"findings": {"type": "array", ...}` |
| `execute(**kwargs)` 的参数名 | `kwargs["findings"]` |

### 3.3 绑定逻辑（LLM 显式传参优先）

- LLM 调用时**已传入**某参数 → 跳过该字段的自动绑定，使用 LLM 提供的值
- LLM 调用时**未传入**某参数 → 触发自动绑定：`ContractBinder.bind_tool_inputs()` → `ProjectionResolver` BFS 搜索 → `ProjectionExecutor` 执行投影链 → `MaterializerRegistry` 物化 → 注入 kwargs

这意味着 `input_fields` 是**兜底机制**，不会覆盖用户/LLM 的显式输入。

### 3.4 materialize_as 速查

| 值 | 适用类型 | `kwargs[name]` 得到 |
|----|---------|---------------------|
| `"string"` | `core.plain_text` | `"第一章 总则\n..."` |
| `"dict"` | 任意 | `{"pages": [...], ...}` |
| `"list_string"` | `core.text_collection` | `["文本1", "文本2"]` |
| `"list_dict"` | 集合类型 | `[{...}, {...}]` |
| `"$.a.b.c"` | 任意（JSONPath） | 嵌套字段的值 |

### 3.5 原理

`ToolRegistry.execute()`（`src/agent/tools/registry.py:161-185`）在执行工具前检测到 `input_fields` 不为空：

```
ContractBinder.bind_tool_inputs(fields, tool_name, explicit_kwargs, artifact_store)
  ├── 识别缺失的必填字段
  ├── ProjectionResolver BFS 搜索投影路径
  ├── ProjectionExecutor 执行投影链
  ├── MaterializerRegistry 物化为工具参数
  └── 注入 kwargs
```

---

## 4. 场景 C：工具既消费又产出

> **效果：** 工具嵌入 Artifact 生产-消费链，自动接收上游数据，产出自动传递给下游。

### 4.1 做法：修改工具类文件（合并 A + B）

**修改的文件：** 你的工具类所在的 `.py` 文件

```python
from src.agent.artifacts.models import InputField
from src.agent.tools.protocol import ToolResult


class ContentAuditTool:
    name = "content_audit"
    description = "对文档内容进行合规性审计"
    parameters = { ... }

    # 消费：需要文档文本
    input_fields: tuple[InputField, ...] = (
        InputField(name="document", artifact_type="core.plain_text", materialize_as="string"),
    )

    # 产出：审计发现列表
    output_artifact_type: str | None = "docaudit.audit_finding_list"

    async def execute(self, **kwargs) -> ToolResult:
        doc_text = kwargs["document"]  # ← 自动绑定注入
        findings = run_compliance_check(doc_text)
        return ToolResult(success=True, data={"findings": findings})  # ← 自动注册
```

只需声明两个属性，不需要改任何其他文件。

---

## 5. 场景 D：注册新的 ArtifactType

> **效果：** 定义一个新的类型名（如 `"myorg.sentiment_analysis"`），之后工具的 `output_artifact_type` 和 `input_fields` 就可以引用它。

### 5.1 涉及的文件操作

| 操作 | 文件 | 说明 |
|------|------|------|
| **新建** | `src/agent/artifacts/schemas_<domain>.py` | Schema 注册模块 |
| **修改** | `src/agent/api/app.py` | 在 `create_app()` 中调用注册函数 |

### 5.2 新建 Schema 注册模块

**新建文件:** `src/agent/artifacts/schemas_custom.py`（文件名可自定义）

```python
"""Custom artifact type schemas for extended domain types."""

from .models import ArtifactSchema, register_artifact_schema


def register_custom_artifact_schemas() -> None:
    """Register all custom artifact type schemas.

    Call this once at application startup, before any tool execution.
    """

    register_artifact_schema(
        "myorg.sentiment_analysis",
        ArtifactSchema(
            schema_version="1.0",
            schema_format="json_schema",
            schema_body={
                "type": "object",
                "properties": {
                    "overall_sentiment": {
                        "type": "string",
                        "enum": ["positive", "negative", "neutral"],
                    },
                    "score": {
                        "type": "number",
                        "minimum": -1.0,
                        "maximum": 1.0,
                    },
                    "aspects": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "aspect": {"type": "string"},
                                "sentiment": {"type": "string"},
                                "score": {"type": "number"},
                            },
                        },
                    },
                },
                "required": ["overall_sentiment", "score"],
            },
            description="情感分析结果",
        ),
    )

    # 可以在此注册更多类型...
```

### 5.3 修改 app.py

**修改文件:** `src/agent/api/app.py`

在 `create_app()` 函数中，`PluginSystem` 初始化之前添加注册调用：

```python
# app.py — 在 create_app() 函数中添加（约 line 130 之前）

from src.agent.artifacts.schemas_custom import register_custom_artifact_schemas

def create_app(...):
    ...
    # ★ 添加这行 — 注册自定义 artifact 类型（必须在 PluginSystem 启动前）
    register_custom_artifact_schemas()

    app.state.artifact_store_registry = SessionArtifactStoreRegistry()

    app.state.plugin_system = PluginSystem(...)
    ...
```

### 5.4 推荐的 Schema 格式

| `schema_format` | 适用场景 |
|---|---|
| `"json_schema"`（推荐） | 通用，支持 `validate_artifact_data()` 自动校验 |
| `"type_hint"` | 轻量场景，不校验 |

### 5.5 原理

Schema 注册表是全局单例（`models.py:32` 的 `_artifact_type_schemas_cache`），`register_artifact_schema()` 是 idempotent 的（后写覆盖）。只需在应用启动时调用一次即可。

---

## 6. 场景 E：注册新的 Projector（类型转换器）

> **效果：** 在已有两个类型之间增加转换边，或在新增的类型和已有类型之间建立桥接。

### 6.1 涉及的文件操作

| 操作 | 文件 | 说明 |
|------|------|------|
| **修改** | `src/agent/artifacts/projectors.py` | 在 `create_default_projector_registry()` 中添加新投影器；或在同文件新增投影函数 |

> **注意：** 当前系统中所有调用点（`binder.py:56`、`get_artifact.py`、`list_artifacts.py`、`loop_hints.py`）都通过 `create_default_projector_registry()` 创建新的 registry 实例。因此，新增 Projector **只能通过修改这个函数来生效**。如果需要支持插件级别的动态投影器注册而不改核心代码，需要先将 registry 提升为应用级单例，这是待做的架构优化。

### 6.2 新增一个投影器（三步走）

**修改文件:** `src/agent/artifacts/projectors.py`

**Step 1 — 在同文件中新增投影函数**（放在 `create_default_projector_registry()` 之前）：

```python
# projectors.py — 在 create_default_projector_registry() 之前添加

def _sentiment_to_plain_text(
    artifact: "Artifact", constraints: dict
) -> tuple[dict, "ProjectionQuality", tuple["ProjectionDiagnostic", ...]]:
    """将情感分析结果转为自然语言描述。"""
    data = artifact.data
    text = (
        f"整体情感: {data['overall_sentiment']} (得分: {data['score']:.2f})\n"
    )
    for aspect in data.get("aspects", []):
        text += (
            f"- {aspect['aspect']}: {aspect['sentiment']}"
            f" ({aspect['score']:.2f})\n"
        )

    return (
        {"text": text, "language": "zh", "source_scope": "sentiment_summary"},
        ProjectionQuality(confidence=1.0, lossiness="lossy", stats={}),
        (),
    )
```

**Step 2 — 在 `create_default_projector_registry()` 函数末尾（`return registry` 之前）注册：**

```python
# projectors.py — 在 create_default_projector_registry() 的 return registry 之前添加

    # === 自定义投影器 ===
    registry.register(
        Projector(
            ProjectorSpec(
                name="myorg.sentiment_analysis.to_plain_text",
                source_type="myorg.sentiment_analysis",
                target_type="core.plain_text",
                owner="myorg",
                quality_score=0.95,
                lossiness="lossy",
                cost="cheap",
                layer="domain",
                stability="stable",
                supported_constraints=("max_chars",),
                description="将情感分析结果转为自然语言文本",
            ),
            _sentiment_to_plain_text,
        )
    )

    return registry
```

**Step 3 — 如使用了新的 ArtifactType，确保已在 app.py 中注册（场景 D）：**

```python
# app.py
register_custom_artifact_schemas()  # 先注册新类型
# → PluginSystem 启动时 create_default_projector_registry() 就能找到新投影器
```

### 6.3 投影函数签名

```python
def projector_fn(
    artifact: Artifact,          # 源工件（不可变，只读）
    constraints: dict,           # 来自 InputField.constraints 或 ProjectionPolicy
) -> tuple[
    dict,                        # 新 data — 必须符合 target_type 的 Schema
    ProjectionQuality,           # {"confidence": float, "lossiness": "lossless"|"lossy"|"summary", "stats": dict}
    tuple[ProjectionDiagnostic, ...],  # 诊断信息，可为空元组 ()
]:
    ...
```

### 6.4 ProjectorSpec 字段参考

| 字段 | 类型 | 说明 |
|------|------|------|
| `name` | `str` | 唯一名称，建议 `"{source}.to.{target}"` |
| `source_type` | `str` | 源 ArtifactType |
| `target_type` | `str` | 目标 ArtifactType |
| `owner` | `str` | 所属模块 |
| `quality_score` | `float` (0~1) | 质量评分，BFS 选路依据 |
| `lossiness` | `"lossless"` / `"lossy"` / `"summary"` | 信息损失程度 |
| `cost` | `"free"` / `"cheap"` / `"moderate"` / `"expensive"` | 计算开销 |
| `layer` | `"core"` / `"domain"` / `"plugin"` / `"local"` | 层级 |
| `stability` | `"stable"` / `"experimental"` / `"deprecated"` / `"disabled"` | 稳定性 |
| `supported_constraints` | `tuple[str, ...]` | 支持的约束参数名 |

### 6.5 稳定性生命周期

```
experimental ──(验证通过)──▶ stable ──(不再推荐)──▶ deprecated ──▶ disabled
```

- `experimental`：BFS 可用但 LLM 不感知 → 适合灰度
- `stable`：全量可用
- `deprecated`：仅 `allow_deprecated=True` 时可用
- `disabled`：从图中移除

---

## 7. 场景 F：Plugin 工具对接

> **效果：** 工具以独立服务运行，自动被 `PluginScanner` 发现、由连接管理器拨号加载，通过 `ProxyTool` 进入 `ToolRegistry`，享有全部自动绑定能力。

### 7.1 涉及的文件操作

| 操作 | 文件路径 | 说明 |
|------|---------|------|
| **新建** | `plugins/{category}/{plugin_name}/plugin.yaml` | 插件清单 |
| **新建** | `plugins/{category}/{plugin_name}/tools.py` | 工具类（声明 artifact 属性） |
| **新建** | `plugins/{category}/{plugin_name}/entry.py` | 插件入口（注册工具） |
| **新建** | `plugins/{category}/{plugin_name}/pyproject.toml` | Python 项目元信息 |
| **无需修改** | — | `PluginScanner` 自动扫描 `plugins/` 目录 |

### 7.2 目录结构

```
plugins/
├── common/          ← 共享工具（parse, search, annotate, template）
│   └── parse/
│       ├── plugin.yaml
│       ├── tools.py
│       ├── entry.py
│       └── pyproject.toml
│
└── audit/           ← 审计领域工具（format_audit, content_audit, plagiarism, ...）
    └── plagiarism/
        ├── plugin.yaml
        ├── tools.py
        ├── entry.py
        └── pyproject.toml
```

**分类规则：**
- 通用能力（解析、搜索、标注）→ `plugins/common/{name}/`
- 审计领域能力（格式审计、内容审计、抄袭检测）→ `plugins/audit/{name}/`

新增一个完全自定义领域的 Plugin 也可以放在 `plugins/{your_category}/{name}/`，`PluginScanner` 会递归扫描所有子目录。

### 7.3 新建 plugin.yaml

**新建文件:** `plugins/audit/my_audit/plugin.yaml`

```yaml
name: my_audit
version: "1.0.0"
api: "1.0"
description: "自定义审计插件"

# 如果不需要直接访问 artifact_store，这个 dependencies 块可省略
dependencies:
  host_services: []
  permissions: []

capabilities:
  tools:
    - name: my_audit
      display_name: 自定义审计
      description: "执行自定义审计检查"
```

> **注意：** `input_fields` 和 `output_artifact_type` **不需要在 plugin.yaml 中声明**。`PluginManager` 通过反射读取 Python 工具类上的属性，自动构建 `ProxyTool`。

### 7.4 新建 tools.py

**新建文件:** `plugins/audit/my_audit/tools.py`

与内置工具写法完全相同：

```python
"""Custom audit tools."""

from typing import Any

from src.agent.artifacts.models import InputField
from src.agent.tools.protocol import ToolResult


class MyAuditTool:
    name = "my_audit"
    description = "执行自定义审计检查"
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "待审计文本"},
        },
        "required": ["text"],
    }

    input_fields: tuple[InputField, ...] = (
        InputField(name="text", artifact_type="core.plain_text", materialize_as="string"),
    )
    output_artifact_type: str | None = "docaudit.audit_finding_list"

    async def execute(self, **kwargs: Any) -> ToolResult:
        text = kwargs["text"]
        findings = do_my_audit(text)
        return ToolResult(success=True, data={"findings": findings})
```

### 7.5 新建 entry.py

**新建文件:** `plugins/audit/my_audit/entry.py`

```python
"""My audit plugin entry point."""

from tools import MyAuditTool

from src.plugin.sdk import PluginRuntime


class MyAuditPlugin(PluginRuntime):
    def register_capabilities(self):
        return {
            "system_prompt": "# 自定义审计\n\n## 能力\n执行自定义审计检查。\n",
        }

    def _setup_handlers(self):
        tool = MyAuditTool()
        self.register_tool(tool)


if __name__ == "__main__":
    import asyncio
    asyncio.run(MyAuditPlugin().run())
```

### 7.6 新建 pyproject.toml

**新建文件:** `plugins/audit/my_audit/pyproject.toml`

```toml
[project]
name = "my-audit"
version = "1.0.0"
requires-python = ">=3.12"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

### 7.7 安装依赖

```bash
cd plugins/audit/my_audit
uv sync
```

### 7.8 不需要修改任何已有文件

`PluginSystem` 在 `app.py:131` 启动时会自动扫描 `plugins/` 目录：

```
PluginScanner.scan("plugins/")
  → 递归搜索所有 plugin.yaml
    → 找到 plugins/audit/my_audit/plugin.yaml
    → 校验 manifest → 标记 VALID
ProcessManager 拨号连接插件服务
  → 运行 entry.py → MyAuditPlugin._setup_handlers()
    → self.register_tool(MyAuditTool())
      → ProxyTool 进入 ToolRegistry
        → 享有与内置工具完全相同的 ContractBinder 自动绑定
```

### 7.9 Plugin 直接访问 ArtifactStore（高级）

如果 Plugin 进程内需要绕过工具契约直接读写 ArtifactStore：

**修改 plugin.yaml：**

```yaml
dependencies:
  host_services:
    - artifact_store
  permissions:
    - read:artifacts
    - write:artifacts
```

**在 Plugin 代码中通过 JSON-RPC 调用（`src/plugin/manager.py` 处理）：**

| 方法 | 对应 handler | 用途 |
|------|------------|------|
| `artifact_store.put` | `manager.py:468` | 写入 Artifact |
| `artifact_store.get` | `manager.py:489` | 按 ID 读取 |
| `artifact_store.list` | `manager.py:508` | 列出全部 |

---

## 8. 高级控制

以上都在工具类上声明类属性即可，不改任何架构文件。

### 8.1 RuntimePolicy — 频次限制

```python
from src.agent.artifacts.models import RuntimePolicy

class ExpensiveTool:
    runtime_policy = RuntimePolicy(max_calls=5, max_consecutive=2)
```

超限时 `ToolRegistry._check_runtime_policy()` 返回错误，不会执行。

### 8.2 skip_persist — 跳过 CacheStore

```python
class VolatileTool:
    output_artifact_type = "core.debug_view"
    skip_persist = True  # 不写入 CacheStore
```

### 8.3 semantic_role — 语义标记

通过 `ToolResult.metadata["label"]` 传递，辅助 LLM 区分多个同类型工件：

```python
return ToolResult(success=True, data=result, metadata={"label": "主文档"})
```

---

## 9. 对接检查清单

### 产出端

- [ ] `output_artifact_type` 的值在 Schema 注册表中存在（见[第 10 节](#101-已有-artifacttype)）
- [ ] 如需新类型 → 场景 D：新建 schema 注册模块 + 修改 `app.py`
- [ ] `result.data` 结构与声明类型的 Schema 一致

### 消费端

- [ ] `InputField.name` = `parameters.properties` 的 key = `execute(**kwargs)` 的参数名
- [ ] `artifact_type` 在 Schema 注册表中存在
- [ ] `materialize_as` 正确反映了需要的参数形式
- [ ] 如 Store 中已有工件类型与输入类型不同 → 场景 E：修改 `projectors.py` 添加投影器

### Plugin 工具

- [ ] 创建了完整的 Plugin 目录：`plugin.yaml` + `tools.py` + `entry.py` + `pyproject.toml`
- [ ] Plugin 目录放在正确的分类下：`plugins/common/` 或 `plugins/audit/`
- [ ] `plugin.yaml` 中 `name` 与目录名一致
- [ ] 如直接访问 ArtifactStore，`plugin.yaml` 中声明了 `artifact_store` 主机服务和权限

### 文件变更汇总

| 场景 | 新建文件 | 修改文件 |
|------|---------|---------|
| A / B / C（已有工具加声明） | 无 | 工具类所在的 `.py`（加 1~2 个类属性） |
| 新建内置工具 | `src/agent/tools/builtin/<tool>.py` | `builtin/__init__.py`、`orch.py` |
| D（新类型） | `src/agent/artifacts/schemas_<name>.py` | `src/agent/api/app.py` |
| E（新投影器） | 无 | `src/agent/artifacts/projectors.py` |
| F（新 Plugin） | `plugins/{cat}/{name}/` 下 4 个文件 | **无需修改已有文件** |

---

## 10. 已有类型与投影器速查

### 10.1 已有 ArtifactType

#### Core 层

| 类型 | 关键字段 | 推荐 materialize_as |
|------|---------|-------------------|
| `core.plain_text` | `text`, `language`, `source_scope` | `"string"`, `"dict"` |
| `core.text_collection` | `items: [{text, metadata}]` | `"list_string"`, `"list_dict"` |
| `core.json_object` | 任意 JSON | `"dict"` |
| `core.error_report` | `errors: [{code, message, location}]` | `"dict"`, `"list_dict"` |
| `core.debug_view` | 调试数据 | `"dict"` |

#### DocAudit 领域层

| 类型 | 典型生产者 |
|------|-----------|
| `docaudit.parsed_document` | `parse_document` |
| `docaudit.paragraph_list` | 投影产物 |
| `docaudit.search_results` | `search` |
| `docaudit.document_metadata` | `parse_document` |
| `docaudit.document_structure` | `parse_document` |
| `docaudit.audit_finding_list` | `format_audit`, `content_audit` |
| `docaudit.audit_report` | `generate_report` |
| `docaudit.reference_text_list` | 投影产物 |
| `docaudit.plagiarism_report` | `detect_plagiarism` |

### 10.2 已有投影器图

```
docaudit.parsed_document ──[0.95]──▶ docaudit.paragraph_list ──[0.98]──▶ core.plain_text
docaudit.search_results   ──[0.92]──▶ docaudit.reference_text_list ──[0.97]──▶ core.text_collection
```

### 10.3 工具声明速查

```python
# 纯生产者
output_artifact_type = "docaudit.parsed_document"

# 纯消费者
input_fields = (
    InputField(name="doc", artifact_type="core.plain_text", materialize_as="string"),
)

# 生产+消费
input_fields = (
    InputField(name="doc", artifact_type="core.plain_text", materialize_as="string"),
)
output_artifact_type = "docaudit.audit_finding_list"

# 带频次限制
runtime_policy = RuntimePolicy(max_calls=5, max_consecutive=2)

# 带约束
input_fields = (
    InputField(name="doc", artifact_type="core.plain_text",
               materialize_as="string",
               constraints={"max_chars": 50000, "normalize_whitespace": True}),
)
```
