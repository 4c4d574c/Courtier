# Artifact 模块与插件子代理交互修复设计

**日期**: 2026-06-09
**状态**: 设计中

## 背景

Artifact 模块提供类型化的数据投影系统（ArtifactStore、ContractBinder、ProjectionExecutor），插件子代理（ProxyAgent）通过 JSON-RPC 与主进程通信。当前两者之间存在 4 个交互断裂点，需要在保持架构一致性的前提下修复。

## 约束

- JSON-RPC 为单向通信（Host → Plugin 请求，Plugin → Host 仅通知），不引入双向 RPC
- 使用「Push + 结果回传」模式替代双向 RPC
- 遵循现有 Proxy 模式（与 ProxyTool 保持一致）
- 遵循不可变性原则（model_copy、tuple 等）

## 问题与修复

### 问题 #1：structured input 无法到达插件子代理

**根因**：`ProxyAgent._HOST_KWARGS` 包含 `"input"`，`dispatch_structured` 传入的 Pydantic `input_obj` 在 JSON-RPC 转发时被过滤。

**修复**：

1. `SubAgentRunner.dispatch_structured()` 在调用 `agent.run()` 前序列化：`input_data = input.model_dump()`
2. 通过新参数 `input_data: dict | None` 传递（不在 `_HOST_KWARGS` 中）
3. `ProxyAgent.run()` 接收 `input_data` 并转发到 JSON-RPC params

**涉及文件**：
- `src/agent/agents/subagent/runner.py` — `dispatch_structured` 序列化
- `src/plugin/proxies.py` — `ProxyAgent.run()` 接收并转发 `input_data`

### 问题 #2：插件子代理看不到 ArtifactStore

**根因**：`artifact_store` 在 `ProxyAgent._HOST_KWARGS` 中，不会转发到插件子进程。插件子进程无法访问类型化 artifact 投影系统。

**修复**：

1. `SubAgentRunner` 增加 `_build_artifact_context()` 方法，从 `scoped_store` 提取 artifact 元数据
2. 格式：`[{"artifact_id": "$ref:parse_document:1", "artifact_type": "docaudit.parsed_document", "projectable_to": ["core.plain_text"], ...}]`
3. 注入到 `dispatch()` / `dispatch_structured()` 的 run_kwargs
4. `ProxyAgent.run()` 转发 `artifact_context` 到 JSON-RPC params

**涉及文件**：
- `src/agent/agents/subagent/runner.py` — `_build_artifact_context()` + dispatch 传参
- `src/plugin/proxies.py` — `ProxyAgent.run()` 转发 `artifact_context`

### 问题 #3：_SubAgentTool 缺少 output_artifact_type

**根因**：`_SubAgentTool.__init__()` 未声明 `output_artifact_type`，子代理结果不会被注册为类型化 artifact。

**修复**：

1. `SubAgentConfig` 增加 `output_artifact_type: str | None = None` 字段
2. `_SubAgentTool.__init__()` 从 config 读取并设置 `self.output_artifact_type`
3. `ToolRegistry.execute()` 已有自动注册逻辑，声明后自动生效
4. `OrchestratorAgent` 中定义插件子代理时设置该字段

**涉及文件**：
- `src/agent/agents/subagent/config.py` — `SubAgentConfig` 增加字段
- `src/agent/agents/subagent/tool.py` — `_SubAgentTool` 读取并暴露
- `src/agent/agents/orch.py` — orchestrator 声明输出类型

### 问题 #4：双向 artifact 流不对称

**修复后的完整数据流**（#1、#2、#3 修复后的自然结果，无额外代码改动）：

```
Host                                     Plugin
────                                     ──────
dispatch 时推送:
  artifact_context ──────────────────▶  插件 LLM 可看到可用 artifact 清单
  input_data ───────────────────────▶  插件收到结构化输入（如有）

插件运行时:
  插件通过 $ref + 磁盘缓存加载数据       （现有机制，无需改动）

插件返回时:
  ◀──────────────────────────────────  agent.run result

Host 侧 _SubAgentTool:
  output_artifact_type 非空时
  → ToolRegistry 自动注册 artifact
```

## 数据流变化对比

### 修复前

| 数据项 | Host→Plugin | Plugin→Host |
|--------|-------------|-------------|
| task (文本) | ✅ | — |
| model_config | ✅ | — |
| structured input | ❌ 被过滤 | — |
| artifact metadata | ❌ 被过滤 | — |
| ProxyTool output → artifact | — | ✅ (host 侧注册) |
| SubAgent output → artifact | — | ❌ 不注册 |

### 修复后

| 数据项 | Host→Plugin | Plugin→Host |
|--------|-------------|-------------|
| task (文本) | ✅ | — |
| model_config | ✅ | — |
| structured input | ✅ (input_data) | — |
| artifact metadata | ✅ (artifact_context) | — |
| ProxyTool output → artifact | — | ✅ |
| SubAgent output → artifact | — | ✅ (output_artifact_type) |

## 实施顺序

1. **问题 #2 先行**：artifact_context 推送 — 为后续修复提供基础
2. **问题 #1**：input_data 转发 — 依赖 #2 建立的数据流模式
3. **问题 #3**：output_artifact_type — 独立改动，最后做
4. **问题 #4**：验证对称性 — 前三步完成后自动验证

## 不做的

- 不引入双向 JSON-RPC（复杂度太高，收益有限）
- 不在插件侧创建独立的 ArtifactStore（避免双重维护）
- 不修改 `cache_store` / `$ref` 机制（已有机制工作正常）
