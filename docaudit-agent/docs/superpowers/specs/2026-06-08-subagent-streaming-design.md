# 子代理流式传输设计

**日期**: 2026-06-08
**状态**: 已确认

## 概述

当前子代理（无论是进程内 SubAgentConfig 还是插件子进程 ProxyAgent）在执行过程中，其内部的思考过程、工具调用、结论生成对前端完全不可见。前端只能看到父代理的 `tool_result` 事件——一个不透明的最终结果块。

本设计定义了一套贯穿所有层的统一流式事件机制，使子代理的实时活动透明地传递到前端。

## 设计决策

| 决策 | 选择 |
|------|------|
| 范围 | 所有子代理（进程内 + 插件） |
| SSE 事件类型 | 新增专用类型（`subagent_*`） |
| JSON-RPC 协议 | 扩展 JSONRPCStreamChunk，新增 `kind` 等字段 |
| 嵌套支持 | 不支持（子代理不可再调度子代理） |
| 架构方案 | 统一流式信封（SubAgentStreamEvent），层间转换最少 |

## 核心数据结构

### SubAgentStreamEvent

```python
from dataclasses import dataclass
from typing import Any, Literal

@dataclass(frozen=True)
class SubAgentStreamEvent:
    """贯穿所有层的统一子代理事件类型。"""
    kind: Literal["token", "think", "tool_result", "conclusion", "start", "end"]
    subagent_name: str
    text: str | None = None          # kind=token, conclusion
    detail: str | None = None        # kind=think (e.g. "tool_calls:parse")
    tool_name: str | None = None     # kind=tool_result
    tool_status: str | None = None   # "ok" | "error"
    tool_duration: float | None = None
    tool_summary: str | None = None
    task: str | None = None          # kind=start
    result: Any = None               # kind=end (AgentResult dict)
```

### JSONRPCStreamChunk（扩展后）

```python
class JSONRPCStreamChunk(BaseModel):
    id: int
    status: Literal["continue", "end"]
    chunk: str | None = None           # 显示文本（保留可读性，向后兼容）
    result: Any = None
    # 以下为新字段（全部可选，默认 None）
    kind: str | None = None            # token|think|tool_result|conclusion|start|end
    subagent_name: str | None = None
    tool_name: str | None = None
    tool_status: str | None = None
    tool_duration: float | None = None
    tool_summary: str | None = None
    task: str | None = None
```

## SSE 事件类型映射

| SubAgentStreamEvent.kind | SSE event type | 携带字段 |
|--------------------------|----------------|----------|
| `start` | `subagent_start` | name, task |
| `token` | `subagent_token` | name, text |
| `think` | `subagent_think` | name, detail |
| `tool_result` | `subagent_tool_result` | name, tool_name, status, duration, summary |
| `conclusion` | `subagent_conclusion` | name, text |
| `end` | `subagent_end` | name, result |

## 数据流

### 路径 A：进程内子代理

```
agent.run(on_step, on_token, on_tool_result, on_content_token)
  → SubAgentRunner 拦截标准回调
  → 转换为 SubAgentStreamEvent
  → _CallbackHolder.on_subagent_event(event)
  → SSEAdapter.on_subagent_event(event)
  → SSE 事件
```

### 路径 B：插件子代理

```
plugin agent.run handler → yield stream_event(...)
  → PluginRuntime._send_chunk() 序列化为 JSONRPCStreamChunk
  → stdout JSONL
  → JSONRPCClient.stream() 解析
  → ProxyAgent.run() 构造 SubAgentStreamEvent
  → _CallbackHolder.on_subagent_event(event)
  → SSEAdapter.on_subagent_event(event)
  → SSE 事件
```

## Plugin SDK API

插件作者在 `agent.run` handler 中使用 `stream_event()` helper：

```python
from plugin_sdk import PluginRuntime, stream_event

class MyPlugin(PluginRuntime):
    def _setup_handlers(self):
        @self.on("agent.run")
        async def handle_agent_run(params):
            name = "parser"
            task = params["task"]

            # 1. 通知宿主子代理启动
            yield stream_event("start", name=name, task=task)

            # 2. 流式输出思考 token
            async for token in llm_stream:
                yield stream_event("token", name=name, text=token)

            # 3. 工具调用
            yield stream_event("think", name=name, detail="tool_calls:parse_document")
            result = await do_parse()
            yield stream_event("tool_result", name=name,
                              tool_name="parse_document", tool_status="ok")

            # 4. 流式输出结论
            yield stream_event("conclusion", name=name, text="解析完成，共5页")

            # 5. 最终结果（generator 最后 yield）
            yield stream_event("end", name=name,
                              result={"status": "completed", "content": "解析完成，共5页"})
```

### stream_event() 签名

```python
def stream_event(
    kind: str,  # "token" | "think" | "tool_result" | "conclusion" | "start" | "end"
    *,
    name: str = "",
    text: str | None = None,
    detail: str | None = None,
    tool_name: str | None = None,
    tool_status: str | None = None,
    tool_duration: float | None = None,
    tool_summary: str | None = None,
    task: str | None = None,
    result: Any = None,
) -> dict:
    """返回一个 dict，PluginRuntime 将其字段映射到 JSONRPCStreamChunk。"""
```

## 回调接口扩展

`_CallbackHolder` 新增一个槽位：

```python
class _CallbackHolder:
    on_step: Callable | None
    on_token: Callable | None
    on_content_token: Callable | None
    on_tool_result: Callable | None
    on_subagent_event: Callable[[SubAgentStreamEvent], Awaitable[None]] | None  # NEW
```

SSEAdapter 新增方法：

```python
async def on_subagent_event(self, event: SubAgentStreamEvent) -> None:
    match event.kind:
        case "start":
            await self._emit_sse({"type": "subagent_start", "name": event.subagent_name, "task": event.task})
        case "token":
            await self._emit_sse({"type": "subagent_token", "name": event.subagent_name, "text": event.text})
        case "think":
            await self._emit_sse({"type": "subagent_think", "name": event.subagent_name, "detail": event.detail})
        case "tool_result":
            await self._emit_sse({"type": "subagent_tool_result", ...})
        case "conclusion":
            await self._emit_sse({"type": "subagent_conclusion", "name": event.subagent_name, "text": event.text})
        case "end":
            await self._emit_sse({"type": "subagent_end", "name": event.subagent_name, "result": event.result})
```

## 改动文件清单

| 文件 | 改动 | 性质 |
|------|------|------|
| `src/plugin/protocol.py` | JSONRPCStreamChunk 新增字段 | 扩展 |
| `src/plugin/sdk/runtime.py` | _send_chunk 支持新字段；stream_event helper | 扩展 |
| `src/plugin/client.py` | stream() 将 chunk 转为 SubAgentStreamEvent | 扩展 |
| `src/plugin/proxies.py` | ProxyAgent 接收回调，解析 chunk 并调用 on_subagent_event | **核心** |
| `src/agent/agents/subagent/config.py` | _CallbackHolder 新增 on_subagent_event | 扩展 |
| `src/agent/agents/subagent/runner.py` | 拦截标准回调 → SubAgentStreamEvent | **核心** |
| `src/agent/api/sse_adapter.py` | on_subagent_event → SSE 映射 | **核心** |
| `src/agent/api/services/stream_service.py` | 传递 on_subagent_event 到 adapter | 接线 |

## 向后兼容

- JSONRPCStreamChunk 所有新字段默认 None——旧插件 chunk 无 kind 时按现有行为收集文本
- PluginRuntime._send_chunk() 只在字段非 None 时序列化
- stream_event() 是新 API，旧插件作者无需迁移
- 现有工具类插件（不涉及 agent.run）完全不受影响

## 错误处理

- **插件崩溃（流中途断开）**：JSONRPCClient 已有 _closed 检查；ProxyAgent 收到异常时发射 subagent_end 带 error
- **心跳超时**：stream() heartbeat_timeout 触发 asyncio.TimeoutError；现有重试逻辑（_execute_with_retry）不变
- **格式异常的 chunk**：未识别 kind 时 WARNING 日志 + 当作纯文本 token 处理

## 测试策略

### 单元测试
- SubAgentStreamEvent 创建和字段验证
- JSONRPCStreamChunk 新旧字段序列化/反序列化兼容
- stream_event() helper 输出格式
- SSEAdapter.on_subagent_event() 各 kind → SSE 映射正确性

### 集成测试
- JSONRPCClient.stream() 解析带 kind 的 chunk
- ProxyAgent 流式 chunk → SubAgentStreamEvent 转换
- SubAgentRunner 标准回调 → SubAgentStreamEvent 转换
- 端到端：插件 subprocess → SSE 事件完整链路

### 回归测试
- 现有纯文本 chunk（无 kind）行为不变
- 现有工具类插件不受影响
- 现有 SSE 事件类型不变

## 实现阶段

1. **Phase 1：数据结构** — SubAgentStreamEvent + JSONRPCStreamChunk 扩展 + stream_event() helper
2. **Phase 2：Plugin SDK + ProxyAgent** — PluginRuntime._send_chunk 支持新字段；ProxyAgent 解析并调用 on_subagent_event
3. **Phase 3：_CallbackHolder + SubAgentRunner** — 新增 on_subagent_event 槽位；进程内子代理回调拦截
4. **Phase 4：SSEAdapter + stream_service** — on_subagent_event → SSE 映射；端到端接线
5. **Phase 5：前端适配** — 消费新 SSE 事件类型（非本次范围）

## 不做的事

- 不支持子代理嵌套
- 不修改 agent.run() 主签名
- 不在 SubAgentStreamEvent 中携带完整 ToolResult 对象（只传摘要字段）
- 不引入异步事件总线或其他中间层
