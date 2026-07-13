# 子代理任务传递优化 — 设计文档

**日期**: 2026-06-01
**状态**: 已确认
**类型**: 架构重构

## 背景

当前子代理任务传递将所有信息（数据、指令、引用）拼接为纯文本 `task` 字符串。LLM 需要从文本中理解并复用 `$ref` 引用、解析格式要求等。这在实践中导致：

- LLM 不可靠地理解文本拼接的 task 指令（核心痛点）
- 子代理之间链式数据传递需要经过 orchestrator 中转
- 调试困难，难以区分是 task 传递还是子代理自身的问题

## 目标

1. **结构化参数传递**：子代理接收 Pydantic 类型化输入，不再依赖文本解析
2. **混合 Ref 解析**：参数级自动解析 + 显式传递共存
3. **输出契约**：子代理声明输出类型，为未来声明式管道铺路
4. **一次性重构**：不保留旧接口向后兼容，所有代码统一迁移

## 非目标

- 本次不实现声明式管线（DAG）
- 不改变 Agent base 的核心循环（agent_loop）
- 不改变 ToolRegistry 的 ref 解析机制（仅移到 _SubAgentTool 层）
- 不改变 CacheStore 基础设施层

---

## 设计

### 1. 输入输出契约模型

#### SubAgentInput（基类）

```python
from pydantic import BaseModel, Field
from typing import Any

class SubAgentInput(BaseModel):
    """所有子代理输入的基础模型。"""
    task: str = Field(description="子代理需要完成的任务描述（自然语言）")
    explicit_inputs: dict[str, str] | None = Field(
        default=None,
        description="显式的 ref_id → 参数名映射。key 为参数名，value 为 $ref:xxx"
    )
```

**设计决策**：
- `task` 字段保留 — 自然语言指令仍然存在，但 LLM 不再需要把数据塞进文本
- `explicit_inputs` — 为复杂场景保留的显式 ref 传递通道
- 具体代理继承此类，添加业务字段

#### 各代理 Input 模型

```python
class FormatAuditorInput(SubAgentInput):
    document: str | dict = Field(
        description="待审计的文档。可传入 $ref:parse_document:1 引用"
    )
    doc_type: str = Field(default="通知", description="要验证的文档类型")

class ContentAuditorInput(SubAgentInput):
    document: str | dict = Field(description="待审计的文档")
    rules: list[str] | None = Field(default=None, description="审计规则列表")

class TransformInput(SubAgentInput):
    sources: list[str] = Field(description="源数据引用，每个元素为 $ref:xxx")
    target_schema: dict | None = Field(default=None, description="目标输出格式的 JSON Schema")
```

#### SubAgentOutput（基类）

```python
class SubAgentOutput(BaseModel):
    """所有子代理输出的基础模型。"""
    summary: str = Field(description="执行结果摘要")
```

**设计决策**：
- Output 模型主要用途是：生成 output_schema 供下游引用、运行时输出验证、为未来管道化提供类型锚点
- 输出模型是可选的 — 不声明 output_model 时行为退化到当前模式

### 2. 混合 Ref 解析机制

两种解析方式在不同层级工作，互补而不重叠。

#### 方式一：参数级自动解析

**触发条件**：Input 模型字段值为 `$ref:xxx` 格式的字符串

**解析时机**：`_SubAgentTool.execute()` 收到 LLM 工具调用参数后、dispatch 前

```
LLM 工具调用参数 → Pydantic 校验 Input model → resolve_refs → dispatch
```

**类型自适应**（复用 CacheStore._load_and_adapt 逻辑）：

| 字段类型     | ref 数据是 dict     | ref 数据是 string        | ref 数据是 list |
|-------------|--------------------|-------------------------|----------------|
| `object`    | 直接用             | JSON.parse 尝试          | 保留 ref 字符串 |
| `string`    | JSON.dumps         | 直接用                   | JSON.dumps     |
| `array`     | 保留 ref 字符串     | JSON.parse 尝试          | 直接用          |
| `int/float/bool` | 保留 ref 字符串 | 尝试 cast                | 保留 ref 字符串 |

#### 方式二：显式传递（explicit_inputs）

LLM 可以显式声明 ref 映射，在 `explicit_inputs` 中传入 ref_id，对应参数名作为 key：

```json
{
  "task": "审计文档格式",
  "explicit_inputs": {"document": "$ref:parse_document:1"}
}
```

系统在 dispatch 前自动将 explicit_inputs 中的 ref 解析到对应字段。

#### 解析失败处理

- ref 指向未知文件 → 保留原始 `$ref:xxx` 字符串
- ref 加载成功但类型无法适配 → 保留原始字符串，子代理收到 warning 日志
- 递归深度超过 32 层 → 停止解析，保留当前值

#### JSON Schema 中的 ref 提示

类型为 `str | dict` 的字段在 LLM 看到的 schema 中自动生成 `anyOf`：

```json
{
  "document": {
    "description": "待审计的文档。可传入 $ref:parse_document:1 引用",
    "anyOf": [
      {"type": "string", "pattern": "^\\$ref:[a-z_]+:\\d+", "description": "缓存引用"},
      {"type": "object", "description": "完整的文档对象"}
    ]
  }
}
```

引导 LLM 知道可以传 ref 字符串代替完整数据对象。

### 3. Dispatch 流程

#### 当前流程

```
LLM → tool_call(task="审计格式", ref_ids=[...], output_for="xxx")
        ↓
_SubAgentTool.execute()
  ├─ 文本拼接：task 中内联 ref_ids 列表
  ├─ output_for 追加：task += "\n\n【输出格式要求】..."
  └─ runner.dispatch(name, task=enhanced_text)
        ↓
SubAgentRunner.dispatch()
  └─ agent.run(task=text, context=flat_dict)
```

#### 新流程

```
LLM → tool_call(task="审计格式", document="$ref:parse_doc:1", doc_type="通知")
        ↓
_SubAgentTool.execute()
  ├─ Input 模型构造：FormatAuditorInput(task=..., document="$ref:...", doc_type="通知")
  ├─ Pydantic 校验
  ├─ ref 解析：document 从缓存加载实际数据 + explicit_inputs 解析
  └─ runner.dispatch(name, input=typed_input_obj)
        ↓
SubAgentRunner.dispatch()
  └─ agent.run(input=typed_input_obj)
```

#### _SubAgentTool 关键变更

- `parameters` 从 `config.input_model.model_json_schema()` 生成，不再手写
- 自动为 `dict | str` 类型字段注入 anyOf ref 提示
- `execute()` 做 Pydantic 校验 + ref 解析后 dispatch
- 旧文本拼接路径（ref_ids/output_for）在迁移完成后移除

#### SubAgentRunner.dispatch 签名变更

```python
# 旧
async def dispatch(self, name: str, task: str, context=None, ...) -> ToolResult:

# 新
async def dispatch(self, name: str, input: SubAgentInput, context_manager=None, ...) -> ToolResult:
```

#### Agent.run() 签名变更

```python
# 当前
async def run(self, task: str, context: dict | None, ...) -> AgentResult:

# 新
async def run(
    self,
    input: SubAgentInput | None = None,
    task: str | None = None,
    context: dict | None = None,
    ...
) -> AgentResult:
```

收到 `input` 时从中提取 task 和结构化数据；同时收到时 `input` 优先。

### 4. 迁移清单

| 代理 | Input 模型 | Output 模型 |
|------|-----------|------------|
| FormatAuditorAgent | `FormatAuditorInput(document, doc_type)` | `FormatAuditOutput(errors, doc_type)` |
| ContentAuditorAgent | `ContentAuditorInput(document, rules?)` | `ContentAuditOutput(issues)` |
| CorrectionAuditorAgent | `CorrectionAuditorInput(document, rules?)` | `CorrectionAuditOutput(corrections)` |
| PlagiarismAuditorAgent | `PlagiarismAuditorInput(document, sources?)` | `PlagiarismAuditOutput(matches)` |
| StyleAuditorAgent | `StyleAuditorInput(document, rules?)` | `StyleAuditOutput(issues)` |
| TransformAgent | `TransformInput(sources, target_schema, task)` | `TransformOutput(ref_id)` |

#### 迁移步骤

1. `SubAgentConfig` 新增 `input_model` 和 `output_model` 字段（默认 None）
2. 为每个代理创建 Input/Output Pydantic 模型
3. 修改 `_SubAgentTool._build_parameters()` — 从 input_model 生成 JSON Schema
4. 修改 `orch.py` 中各代理的 `SubAgentConfig` 定义
5. 修改 `SubAgentRunner.dispatch` 签名
6. 清理文本拼接逻辑（`ref_ids` 拼接、`output_for` schema 追加）
7. 更新 `OrchestratorAgent` system prompt，引导 LLM 使用结构化字段

#### 不做的事

- 不改 `agent_loop` — 它只关心 messages 和 tool calls
- 不改 `ToolRegistry` — ref 解析从 ToolRegistry 移到 _SubAgentTool 层
- 不改 `CacheStore` — 已经是干净的基础设施层

### 5. System Prompt 变更

**当前（orchestrator）**：
> 将 ref_id 字符串作为参数值传入即可，系统会自动加载完整数据替换引用

**新（orchestrator）**：
> 每个审计工具的参数中，数据字段接受两种传值方式：
> 1. 传入 `$ref:parse_document:1` 引用字符串，系统自动加载完整数据
> 2. 传入完整的 JSON 对象（仅当数据较小时）
> 推荐使用方式 1，避免上下文膨胀。

子代理的 system prompt 不再混入数据上下文（如 `parsed_doc_summary`），这些数据作为 Input 模型字段传递。

---

## 影响范围

### 修改的文件

| 文件 | 变更程度 | 说明 |
|------|---------|------|
| `src/agent/agents/subagent.py` | 高 | SubAgentConfig 新增字段、_SubAgentTool 重构、dispatch 签名变更 |
| `src/agent/agents/orch.py` | 中 | SubAgentConfig 定义更新、system prompt 更新 |
| `src/agent/agents/transform.py` | 低 | 不直接改，Input 模型单独定义 |
| `src/agent/agents/base.py` | 低 | Agent.run() 签名新增 input 参数 |
| `src/agent/agents/format_auditor.py` | 低 | 不直接改，Input 模型单独定义 |
| `src/agent/agents/content_auditor.py` | 低 | 同上 |
| `src/agent/agents/correction_auditor.py` | 低 | 同上 |
| `src/agent/agents/plagiarism_auditor.py` | 低 | 同上 |
| `src/agent/agents/style_auditor.py` | 低 | 同上 |
| `tests/agent/test_subagent.py` | 高 | 测试更新以覆盖新路径 |

### 新增的文件

| 文件 | 说明 |
|------|------|
| `src/agent/agents/input_models.py` | 所有代理的 Input/Output Pydantic 模型 |

### 不变的文件

| 文件 | 原因 |
|------|------|
| `src/agent/core/loop.py` | agent_loop 不感知子代理 |
| `src/agent/core/state.py` | AgentState 不变 |
| `src/agent/tools/registry.py` | ref 解析移到 _SubAgentTool 层 |
| `src/agent/tools/protocol.py` | ToolProtocol/ToolResult 不变 |
| `src/agent/core/cache_store.py` | 基础设施层，继续复用 |
| `src/agent/core/context_manager.py` | context_manager 仍然传递给子代理 |

---

## 风险与缓解

| 风险 | 缓解 |
|------|------|
| Pydantic JSON Schema 不够灵活 | `Field(description=...)` 提供足够的 LLM 引导信息 |
| anyOf 类型可能让 LLM 困惑 | 通过 system prompt 中的推荐策略引导 |
| 一次性重构改动大 | 按迁移步骤逐步 commit，每步可独立验证 |
| output_model 可能约束过强 | output_model 为可选字段，不声明时行为退化 |
