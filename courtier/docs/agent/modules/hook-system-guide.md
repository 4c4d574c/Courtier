# Hook 系统使用文档

## 目录

1. [概述](#概述)
2. [核心概念](#核心概念)
3. [两种 Handler 类型](#两种-handler-类型)
4. [API 参考](#api-参考)
5. [事件类型](#事件类型)
6. [使用示例](#使用示例)
7. [高级模式](#高级模式)
8. [最佳实践](#最佳实践)
9. [常见问题](#常见问题)

---

## 概述

Hook 系统是 DocAudit Agent 的运行时扩展机制。它允许外部代码在 Agent 主循环的关键节点插入自定义逻辑，而无需修改核心引擎。

你可以把 Hook 理解为 Agent 循环的"中间件"：在特定时机触发、接收当前上下文、执行你的业务逻辑（修改状态或记录副作用）。

### 适用场景

| 场景 | 适合的 Handler 类型 | 推荐事件 |
|---|---|---|
| 安全护栏（限制步数、敏感词过滤） | Interceptor | `pre_think` |
| 审计日志 | Observer | `post_observe` |
| 上下文注入/提示词增强 | Interceptor | `pre_think` |
| 搜索查询改写 | Observer | `pre_search` |
| 工具执行审计 | Observer | `post_observe` |
| 错误/终止处理 | Interceptor | `pre_think` / `post_observe` |

---

## 核心概念

### HookChain

HookChain 是 handler 的注册中心和调度器。一个 Chain 可以注册多个事件、多个 handler，然后传给 Agent 使用。

```python
from src.agent.hooks import HookChain, PRE_THINK

chain = HookChain()
chain.register(PRE_THINK, my_handler)

agent = Agent(..., hooks=chain)
```

### HookContext

Handler 接收的上下文对象，包含当前状态和环境信息。

| 字段 | 类型 | 说明 |
|---|---|---|
| `event` | `str` | 触发的事件名 |
| `state` | `AgentState` | 当前 Agent 状态（不可变） |
| `agent_name` | `str` | 当前 Agent 名称 |
| `session_id` | `str` | 会话 ID |
| `current_step` | `int` | 当前步数 |
| `metadata` | `dict[str, Any]` | 自定义元数据 |

### AgentState

Agent 的不可变状态对象。Hook 中如需修改状态，必须返回新的 `AgentState`：

```python
return ctx.state.model_copy(update={
    "status": "completed",
    "termination_reason": "custom reason",
})
```

---

## 两种 Handler 类型

### Interceptor（拦截器）

- **签名**：`async def handler(ctx: HookContext) -> AgentState`
- **特点**：返回新的 `AgentState`，多个 Interceptor 按优先级 pipeline 执行
- **用途**：修改状态、安全终止、上下文注入

```python
async def guard_max_steps(ctx: HookContext) -> AgentState:
    if ctx.current_step >= 50:
        return ctx.state.model_copy(update={
            "status": "completed",
            "termination_reason": "guard: max steps reached",
        })
    return ctx.state

chain.register(PRE_THINK, guard_max_steps, priority=100)
```

### Observer（观察者）

- **签名**：`async def observer(ctx: HookContext) -> None`
- **特点**：不返回任何值，不修改状态，fire-and-forget
- **用途**：日志、审计、指标、通知

```python
async def audit_thinking(ctx: HookContext) -> None:
    logger.info(
        "agent=%s session=%s step=%d event=%s",
        ctx.agent_name,
        ctx.session_id,
        ctx.current_step,
        ctx.event,
    )

chain.register_observer(PRE_THINK, audit_thinking)
```

**执行顺序**：`run()` 调用时，先执行所有 Interceptor（按 priority 排序），再执行所有 Observer（按注册顺序）。

---

## API 参考

### HookChain 构造

```python
HookChain(
    handler_timeout: float | None = None,  # 链级 handler 超时
    continue_on_error: bool = True,         # 默认是否捕获 handler 异常
)
```

### 注册方法

```python
# Interceptor
handle = chain.register(
    event: str,
    handler: HookHandler,
    *,
    priority: int = 0,           # 数值越大越先执行
    timeout: float | None = None, # 单个 handler 超时
)

# Observer
handle = chain.register_observer(
    event: str,
    handler: HookObserver,
)

# 取消注册
handle.cancel()  # 幂等
```

### 设置上下文

```python
chain.set_context(
    agent_name="MyAgent",
    session_id="sess-123",
    custom_key="custom_value",  # 进入 metadata
)
```

### 触发

```python
final_state = await chain.run(event, state)
final_state = await chain.run(event, state, continue_on_error=False)
```

---

## 事件类型

当前系统预定义了三个事件：

| 事件常量 | 字符串 | 触发位置 | 触发时机 |
|---|---|---|---|
| `PRE_THINK` | `"pre_think"` | `agent_loop` | 每轮循环开始，Think 阶段之前 |
| `POST_OBSERVE` | `"post_observe"` | `agent_loop` | 工具执行+观察+上下文压缩之后 |
| `PRE_SEARCH` | `"pre_search"` | `AssistantAgent.chat_stream` | ES/RAG 搜索之前 |

**提示**：事件名仍可使用裸字符串，但建议使用常量以提高类型安全。

---

## 使用场景

Hook 系统的使用位置决定了你能拦截到的时机和数据。下面按典型的使用位置给出场景说明。

### 1. 在开发插件时

插件本身是独立进程，无法直接操作主进程的 `HookChain`。但插件注册到主进程的能力（`ProxyTool` / `ProxyAgent`）会被主循环调用，因此可以在**宿主代码加载插件时**注册 hook，用来：

- 审计插件工具的调用与返回
- 对插件产出的结果做二次校验
- 在插件工具执行前后注入上下文提示

```python
# 在 PluginSystem 启动后、Agent 运行前注册
chain.register_observer("post_observe", plugin_audit_logger)
```

### 2. 在定义子智能体时

`SubAgentConfig` 中的 `agent` 字段可以接受一个带 `HookChain` 的 Agent 实例。子智能体独立的 hook 链适合：

- 记录子代理内部每轮思考与工具执行
- 对子代理设置单独的步数/安全护栏
- 在子代理返回前统一格式化输出

```python
sub_chain = HookChain()
sub_chain.register("pre_think", subagent_guard, priority=100)

config = SubAgentConfig(
    agent=Agent(..., hooks=sub_chain),
    ...
)
```

### 3. 在编排 Agent 中

`OrchestratorAgent` 统筹整个审核流水线，它的 hook 链可以观测到：

- 每个子代理被调用前后的状态
- 工具调用的整体序列
- 流水线是否陷入探索循环

典型用途：

- 全局审计：记录完整审核流程
- 流程控制：在异常时提前终止整个编排
- 指标上报：按会话统计各阶段耗时

### 4. 在 API / SSE 服务中

在 `AgentService` 或 `StreamService` 构建 Agent 时注入 hook，可以实现：

- 会话级上下文校验（如检查用户权限、会话状态）
- 将 Agent 事件同步写入 `SessionStore`
- 对 SSE 流中的敏感内容进行实时过滤

```python
# 在 AgentService 中按会话构造 HookChain
session_chain = HookChain()
session_chain.set_context(session_id=session_id, agent_name="AuditAgent")
session_chain.register_observer("post_observe", session_logger)
```

### 5. 在 RAG / 对话助手中

`AssistantAgent` 在搜索前触发 `pre_search`，这是 RAG 场景的核心扩展点：

- 将用户口语化问题改写为精确检索词
- 注入领域过滤条件
- 记录检索历史用于后续追问推荐

```python
async def rewrite_search_query(ctx):
    # 改写 query，写入共享变量或调用外部服务
    ...

assistant = AssistantAgent(client=client, hooks=chain)
chain.register_observer("pre_search", rewrite_search_query)
```

### 6. 在权限与合规控制中

在 `Agent.__init__` 或 `AgentService` 中统一注册安全类 hook：

- 敏感词/敏感文件类型过滤
- 步数上限、token 上限强制终止
- 合规审计：记录谁、在什么时候、调用了什么工具

```python
async def compliance_guard(ctx):
    if has_sensitive_content(ctx.state.messages):
        return ctx.state.model_copy(update={
            "status": "blocked",
            "termination_reason": "compliance: sensitive content",
        })
    return ctx.state

chain.register("pre_think", compliance_guard, priority=1000)
```

### 7. 在本地测试与调试中

Hook 是测试 Agent 内部状态的有力工具：

- 断言某一轮是否调用了指定工具
- 在测试中注入 Mock 状态
- 打印每轮 state 变化，辅助定位无限循环

```python
# 测试用 hook
async def capture_steps(ctx):
    captured_steps.append(ctx.current_step)

chain.register_observer("post_observe", capture_steps)
```

---

## 使用示例

### 示例 1：基础使用

```python
from src.agent.hooks import HookChain, PRE_THINK, POST_OBSERVE
from src.agent.agents.base import Agent

chain = HookChain()

async def log_step(ctx):
    print(f"[{ctx.event}] agent={ctx.agent_name} step={ctx.current_step}")
    return ctx.state

chain.register(PRE_THINK, log_step)

agent = Agent(
    name="AuditAgent",
    role="你是一个文档审查助手...",
    tools=[...],
    model=model_client,
    hooks=chain,
)

result = await agent.run("审查这份合同")
```

### 示例 2：安全护栏

```python
async def sensitive_word_guard(ctx):
    """检测到敏感词直接终止会话。"""
    for msg in ctx.state.messages:
        if msg.content and "密码" in msg.content:
            return ctx.state.model_copy(update={
                "status": "blocked",
                "termination_reason": "sensitive content detected",
            })
    return ctx.state

chain.register(PRE_THINK, sensitive_word_guard, priority=999)
```

### 示例 3：审计日志 Observer

```python
import logging

logger = logging.getLogger(__name__)

async def audit_observer(ctx):
    logger.info({
        "event": ctx.event,
        "agent": ctx.agent_name,
        "session": ctx.session_id,
        "step": ctx.current_step,
        "status": ctx.state.status,
    })

chain.register_observer(POST_OBSERVE, audit_observer)
```

### 示例 4：RAG 搜索查询改写

```python
async def enhance_search(ctx):
    """pre_search 是 Observer，只做副作用。"""
    # 可以在这里记录查询、改写查询、添加过滤条件等
    logger.info("Search task: %s", ctx.state.messages[-1].content)

chain.register_observer(PRE_SEARCH, enhance_search)

assistant = AssistantAgent(client=client, hooks=chain)
```

### 示例 5：带超时的 Handler

```python
async def slow_policy_check(ctx):
    # 调用外部策略服务
    await call_external_policy_api(ctx.state)
    return ctx.state

# 单个 handler 最多 5 秒
chain.register(PRE_THINK, slow_policy_check, timeout=5.0)
```

---

## 高级模式

### 1. Handler 间状态传递

Interceptor 按 pipeline 执行，后面的 handler 可以看到前面 handler 修改后的 state：

```python
async def tag_step(ctx):
    return ctx.state.model_copy(update={"termination_reason": "tagged"})

async def read_tag(ctx):
    if ctx.state.termination_reason == "tagged":
        # 执行额外逻辑
        pass
    return ctx.state

chain.register(PRE_THINK, tag_step, priority=10)
chain.register(PRE_THINK, read_tag, priority=5)
```

### 2. 条件注册/动态取消

```python
handle = chain.register(POST_OBSERVE, temporary_handler)

# 满足条件后移除
def on_condition_met():
    handle.cancel()
```

### 3. 优先级排序

```python
# 高优先级先执行
chain.register(PRE_THINK, security_guard, priority=100)
chain.register(PRE_THINK, context_injector, priority=50)
chain.register(PRE_THINK, metric_collector, priority=10)

# 同优先级保持注册顺序
chain.register(PRE_THINK, a, priority=0)  # 先
chain.register(PRE_THINK, b, priority=0)  # 后
```

### 4. 严格模式（异常向上传播）

默认情况下，handler 异常会被捕获并记录，不影响后续 handler。如需旧行为：

```python
chain = HookChain(continue_on_error=False)
# 或单次调用
await chain.run(PRE_THINK, state, continue_on_error=False)
```

### 5. 与 Agent 生命周期绑定

```python
# Agent.run() 会自动调用 hooks.set_context(agent_name=..., session_id=...)
# AssistantAgent.chat_stream() 会自动设置 agent_name="AssistantAgent"
```

---

## 最佳实践

### 1. 不要直接修改 `ctx.state`

`AgentState` 是 frozen/Pydantic 不可变对象，直接赋值会静默失败或报错。始终使用 `model_copy(update=...)`：

```python
# ❌ 错误
ctx.state.status = "completed"

# ✅ 正确
return ctx.state.model_copy(update={"status": "completed"})
```

### 2. Observer 用于纯副作用

如果不需要修改 state，优先使用 Observer。这样代码意图更清晰，且 Observer 不影响主循环状态：

```python
# 好
chain.register_observer(POST_OBSERVE, audit_logger)

# 不推荐
chain.register(POST_OBSERVE, audit_logger_returning_state)
```

### 3. 高优先级用于安全护栏

安全类 handler 应设置较高 priority，确保它们在其他逻辑之前执行：

```python
chain.register(PRE_THINK, safety_guard, priority=1000)
chain.register(PRE_THINK, context_enhancer, priority=100)
```

### 4. 避免在 Handler 中执行阻塞操作

所有 handler 都是 async 的。如需执行同步 IO，应使用 `asyncio.to_thread` 或异步客户端。同时合理使用 `timeout`：

```python
async def check_policy(ctx):
    # 同步调用改为在线程池执行
    result = await asyncio.to_thread(sync_policy_check, ctx.state)
    return ctx.state

chain.register(PRE_THINK, check_policy, timeout=3.0)
```

### 5. 错误处理策略

- **Interceptor**：失败可能影响整个 Agent，谨慎使用 `continue_on_error=False`
- **Observer**：失败应始终安全，系统会自动捕获和记录

### 6. 保持 Handler 简单

一个 handler 只做一件事。复杂逻辑拆成多个 handler，通过 priority 组合：

```python
chain.register(PRE_THINK, validate_input, priority=200)
chain.register(PRE_THINK, inject_hints, priority=100)
chain.register(PRE_THINK, collect_metrics, priority=10)
```

---

## 常见问题

### Q1：注册的事件名打错了会怎样？

不会报错，但 handler 永远不会被触发。建议导入常量：

```python
from src.agent.hooks import PRE_THINK  # 推荐
```

### Q2：Observer 能否修改 state？

不能。Observer 的返回值会被忽略。如果看到 state 被修改，请检查是否误注册为 Interceptor。

### Q3：多个 Agent 能否共享同一个 HookChain？

可以，但不推荐并发运行。`set_context()` 会覆盖上下文。每个 Agent 实例使用独立的 HookChain 更安全。

### Q3.5：同一个 handler 对象注册多次会怎样？

`unregister`（通过 `HookHandle.cancel()` 调用）使用对象引用相等判断。如果同一个函数对象被注册多次，`cancel()` 只会移除第一次注册的那个实例。通常情况下应避免重复注册同一个 handler。

### Q4：timeout 超时后会怎样？

默认（`continue_on_error=True`）：记录 warning，继续执行后续 handler。
严格模式（`continue_on_error=False`）：抛出 `asyncio.TimeoutError`。

### Q5：如何让 hook 在 Agent 出错时也执行？

当前系统没有 `on_error` 事件。可以在 `post_observe` 中检查 `ctx.state.status`，或等待 Phase 3 扩展事件点。

### Q6：handler 中抛出异常会被吞掉吗？

不会。默认情况下会记录到日志（`logger.exception`），并继续执行后续 handler。日志中可查看到完整堆栈。

---

## 相关文件

- 实现：`src/agent/hooks/chain.py`
- 导出：`src/agent/hooks/__init__.py`
- 集成：`src/agent/core/loop.py`、`src/agent/agents/assistant.py`
- 测试：`tests/agent/test_hooks.py`、`tests/agent/test_assistant_agent.py`
