# 插件 Agent Loop 设计

**日期**: 2026-06-08
**状态**: 已确认

## 概述

当前所有插件的 `agent.run` handler 是空壳——只 echo 任务描述，不调用 LLM，不执行工具，不返回实际审计结果。

本设计在 `PluginRuntime` 中内置 `run_agent()` 方法，提供完整的 LLM agent loop（think → tool_call → observe），使插件子进程能自主完成推理和工具编排。

依赖前序设计：[子代理流式传输](./2026-06-08-subagent-streaming-design.md)（`stream_event()` 和 `SubAgentStreamEvent` 已就绪）。

## 设计决策

| 决策 | 选择 |
|------|------|
| LLM 配置来源 | 宿主通过 `agent.run` params 传入（`api_key`, `base_url`, `model`） |
| Agent loop 实现 | `PluginRuntime.run_agent()` 内置方法，一行委托 |
| 系统提示词 | `register_capabilities()` 的 system_prompt + 自动注入工具列表 |
| LLM 客户端 | `openai.AsyncOpenAI`（与宿主一致） |

## 核心接口

### PluginRuntime.run_agent()

```python
from collections.abc import AsyncIterator

async def run_agent(
    self,
    *,
    name: str,
    task: str,
    model_config: dict[str, str],  # {"api_key": ..., "base_url": ..., "model": ...}
    max_turns: int = 10,
) -> AsyncIterator[dict[str, Any]]:
    """运行 LLM agent loop，yield stream_event() 兼容的 dict。

    插件 author 在 agent.run handler 中一行委托：
        async for event in self.run_agent(name=..., task=..., model_config=...):
            yield event
    """
```

### 插件 author 视角

```python
class FormatAuditPlugin(PluginRuntime):
    def _setup_handlers(self):
        # tool.execute handler 不变
        @self.on("tool.execute")
        async def handle_tool_execute(params): ...

        # agent.run handler 简化为一行委托
        @self.on("agent.run")
        async def handle_agent_run(params):
            async for event in self.run_agent(
                name=params["agent"],
                task=params["task"],
                model_config=params["model_config"],
            ):
                yield event
```

## 内部实现

### run_agent() 流程

```
1. 构建系统提示词
   ↓
2. 创建 AsyncOpenAI client
   ↓
3. 初始化 messages = [system_prompt, user_task]
   ↓
4. yield stream_event("start", name, task)
   ↓
5. for turn in range(max_turns):
   a. 流式调用 LLM（带工具定义）
   b. 对每个 delta yield stream_event("token", text=...)
   c. 若 LLM 返回 tool_calls:
      - yield stream_event("think", detail="tool_calls:xxx,yyy")
      - 逐个执行工具
      - yield stream_event("tool_result", ...)
      - 工具结果加入 messages
   d. 若 LLM 返回纯文本（无 tool_calls）:
      - yield stream_event("conclusion", text=...)
      - break
   ↓
6. yield stream_event("end", name, result={status, content})
```

### 辅助方法

| 方法 | 职责 |
|------|------|
| `_build_full_system_prompt()` | `register_capabilities()` 的 system_prompt + 自动生成工具列表文本 |
| `_build_tool_schemas()` | 从 `register_tool()` 注册的工具生成 OpenAI function-calling 格式 |
| `_collect_stream(stream, name)` | 消费 LLM 流式响应，yield token 事件，返回累积文本和 tool_calls |
| `_execute_tool(name, args)` | 按工具名找到注册实例，执行并返回结果 dict |

### _build_tool_schemas() 输出格式

```python
# 自动从 register_tool() 注册的实例中提取
[
    {
        "type": "function",
        "function": {
            "name": "detect_document_type",
            "description": "检测文档类型...",
            "parameters": {"type": "object", "properties": {...}},
        }
    },
    ...
]
```

### _build_full_system_prompt() 输出格式

```
[插件 register_capabilities() 返回的 system_prompt]

# 可用工具
## detect_document_type
检测文档类型
参数: {"type": "object", "properties": {...}}

## audit_format
格式审计
参数: {"type": "object", "properties": {...}}
```

### _execute_tool() 逻辑

复用 `_setup_handlers` 中注册的工具实例（`register_tool()` 已存储），按名称查找并执行：

```python
async def _execute_tool(self, tool_name: str, args: dict) -> dict:
    tool = self._tool_instances.get(tool_name)
    if tool is None:
        return {"success": False, "error": f"Unknown tool: {tool_name}"}
    result = await tool.execute(**args)
    return {"success": result.success, "data": result.data, "error": result.error}
```

### _collect_stream() 逻辑

消费 OpenAI 流式响应，对每个 content delta yield `stream_event("token")`，同时累积 tool_calls。OpenAI 流式返回 tool_calls 时分多段传输（index → id → function.name → function.arguments），需按 index 组装：

```python
async def _collect_stream(self, stream, name: str) -> tuple[str, list[dict]]:
    thinking_parts: list[str] = []
    tool_calls_by_index: dict[int, dict] = {}
    async for chunk in stream:
        delta = chunk.choices[0].delta
        if delta.content:
            thinking_parts.append(delta.content)
            yield stream_event("token", name=name, text=delta.content)
        for tc in delta.tool_calls or []:
            idx = tc.index
            if idx not in tool_calls_by_index:
                tool_calls_by_index[idx] = {"id": "", "name": "", "arguments": ""}
            entry = tool_calls_by_index[idx]
            if tc.id:
                entry["id"] = tc.id
            if tc.function:
                if tc.function.name:
                    entry["name"] += tc.function.name
                if tc.function.arguments:
                    entry["arguments"] += tc.function.arguments
    tool_calls = [
        {"id": tc["id"], "name": tc["name"], "arguments": tc["arguments"]}
        for tc in tool_calls_by_index.values()
    ]
    return "".join(thinking_parts), tool_calls
```

### register_tool() 扩展

`register_tool()` 除了存储 capability 声明外，还需保存工具实例引用以便 `_execute_tool()` 调用。在 `__init__` 中新增 `self._tool_instances: dict[str, Any] = {}`，`register_tool()` 中增加：

```python
self._tool_instances[tool_instance.name] = tool_instance
```

## 宿主→插件参数传递

### ProxyAgent.run() 转发 model_config

```python
# ProxyAgent.run() 中，提取 model_config 并转发到插件
async for chunk in self._client.stream("agent.run", {
    "agent": self._agent_name,
    "task": task,
    "model_config": kwargs.get("model_config", {}),
    **{k: v for k, v in kwargs.items() if k not in self._HOST_KWARGS},
}):
```

### stream_service 传入 model_config

`generate_sse_stream` 在调用 `agent.run()` 时传入 LLM 配置（从 settings 中获取 api_key、base_url、model）。

### 流式事件自动桥接

`PluginRuntime._send_chunk()` 已将 `stream_event()` dict 映射到 `JSONRPCStreamChunk` 字段。`ProxyAgent.run()` 已将结构化 chunk 转换为 `SubAgentStreamEvent` 并调用 `on_subagent_event`。`SSEAdapter.on_subagent_event()` 已映射到 `subagent_*` SSE 事件。**本设计无需修改流式传输基础设施。**

## 改动文件清单

| 文件 | 改动 | 性质 |
|------|------|------|
| `src/plugin/sdk/runtime.py` | `run_agent()` + `_build_full_system_prompt()` + `_build_tool_schemas()` + `_collect_stream()` + `_execute_tool()` + `_tool_instances` 存储 | **核心** (~150行) |
| `src/plugin/proxies.py` | ProxyAgent.run() 转发 model_config | 小改 (~3行) |
| `src/agent/api/services/stream_service.py` | 传入 model_config 到 agent.run | 小改 (~3行) |
| `plugins/*/entry.py` (5个) | agent.run handler 改用 `run_agent()` | 简化 |

## 不做的事

- 不引入宿主 agent_loop 的复杂依赖（HookChain、PermissionGate、AuditLogger、ContextManager）
- 不支持插件端自定义循环控制参数（除 max_turns 外）
- 不改变 host→plugin 的 JSON-RPC 协议格式
- 不修改 `tool.execute` handler — 现有工具调度逻辑不变

## 测试策略

### 单元测试
- `_build_full_system_prompt()` 包含 system_prompt + 工具列表
- `_build_tool_schemas()` 正确生成 OpenAI function-calling 格式
- `_execute_tool()` 正确分发到注册的工具实例
- `run_agent()` 在无工具调用时直接返回结论

### 集成测试
- 使用 mock LLM 响应验证完整 agent loop 的事件产出顺序（start → token → conclusion → end）
- 使用 mock LLM 返回 tool_call，验证 think → tool_result 事件序列
- 验证 model_config 从 ProxyAgent 到插件 subprocess 的完整传递

### 回归测试
- 现有 tool.execute handler 行为不变
- 现有插件注册流程不变
