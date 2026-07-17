# Courtier 借鉴 Pi 架构的实施计划

> **当前状态**：Phase 1–4 核心代码与运营化补充（会话树 API、metrics/alert、前端适配、工具版本 SOP）已全部落地，对应单元测试通过。详见
> [`pi-architecture-migration-gaps-solutions.md`](./pi-architecture-migration-gaps-solutions.md)。
> **下次评审**：全量回归测试与灰度验证。

> 本文档基于对 [Pi](https://pi.ai) 公开架构理念与 Courtier 当前代码库的对比分析，提出一套分阶段、可回滚的迁移方案。目标是提升 Courtier 在**事件通信、LLM 抽象、状态管理、扩展系统、记忆分层、会话树**等维度的可扩展性与可维护性，同时保持现有 FastAPI/SSE API 向后兼容。

---

## 目录

1. [目标与背景](#1-目标与背景)
2. [Pi 架构核心优势回顾](#2-pi-架构核心优势回顾)
3. [总体迁移原则](#3-总体迁移原则)
4. [实施路线图总览](#4-实施路线图总览)
5. [Phase 1：统一事件总线与 SSE 解耦（0–4 周）](#phase-1统一事件总线与-sse-解耦04-周)
6. [Phase 2：LLM 抽象层与状态协议化（4–8 周）](#phase-2llm-抽象层与状态协议化48-周)
7. [Phase 3：扩展系统与记忆分层（8–14 周）](#phase-3扩展系统与记忆分层814-周)
8. [Phase 4：会话树、工具版本化与高级护栏（14–20 周）](#phase-4会话树工具版本化与高级护栏1420-周)
9. [模块依赖与实施顺序](#9-模块依赖与实施顺序)
10. [风险识别与回滚策略](#10-风险识别与回滚策略)
11. [附录 A：Pi → Courtier 概念映射表](#附录-api--courtier-概念映射表)
12. [附录 B：新增/改造文件清单](#附录-b新增改造文件清单)

---

## 1. 目标与背景

### 1.1 现状痛点

当前 `courtier` 的核心交互链路大致如下：

```text
FastAPI Route → AgentService → agent_loop() → SSEAdapter → HTTP SSE
```

该链路在实践中暴露出以下问题：

| 痛点 | 具体表现 | 影响 |
|------|---------|------|
| **SSE 与事件生成耦合** | `sse_adapter.py` 既负责把循环回调转换为 SSE 事件，又负责过滤/格式化，导致新增事件类型需要改两处 | 前端协议不稳定，扩展成本高 |
| **状态转换隐式** | `AgentState.status` 在 `loop.py` 多处被直接赋值，`thinking → waiting_for_tool → observing` 的转换规则散落在 `think_phase` 与 `execute_tools_phase` 中 | 难以做状态审计、回滚与重放 |
| **LLM 后端硬编码** | `ModelClient` 直接绑定单一模型家族，切换模型需要改构造逻辑 | 无法做模型路由、降级与 A/B 测试 |
| **扩展能力边界模糊** | `Plugin` 与 `Skill` 虽然已分层，但生命周期管理、事件作用域、错误传播仍缺少统一契约 | 子代理事件可能泄漏到父会话 |
| **上下文管理单一** | `ContextManager` 仅做预算控制，未区分系统提示、长期记忆、工作记忆、工具结果缓存 | 长会话压缩策略粗糙 |
| **会话无分支能力** | `AgentState` 是线性消息列表，无法支持“从某一轮重新生成”“并行尝试多种 Skill”等场景 | 审核流程难以回溯优化 |
| **工具协议无版本** | `ToolProtocol` 未声明 schema 版本，插件升级后难以平滑迁移 | 破坏性变更风险高 |

### 1.2 借鉴目标

参考 Pi 架构的以下优势：

1. **统一事件总线**：所有内部状态变更先落事件，传输层（SSE/WebSocket）只是订阅者之一。
2. **显式状态机**：LLM 调用、工具执行、权限检查都是状态转换，可被记录、回放、审计。
3. **模型后端抽象**：模型调用通过统一协议路由，支持多后端、流式、降级。
4. **声明式扩展系统**：能力以 schema/contract 形式注册，运行时只消费契约。
5. **分层记忆系统**：区分工作记忆、短期记忆、长期记忆与外部检索上下文。
6. **会话树模型**：对话支持分支、快照、回滚，便于探索与重试。
7. **工具协议版本化**：工具 schema 带版本，支持多版本共存与灰度。
8. **分层护栏**：输入、输出、工具调用分阶段进行安全与业务护栏检查。

---

## 2. Pi 架构核心优势回顾

Pi 的公开技术分享（以及可观察到的产品行为）体现出以下架构特征，与 Courtier 的对应关系见附录 A：

| Pi 特征 | 对 Courtier 的启示 | 关键收益 |
|--------|-------------------|---------|
| **Event Sourcing Lite** | 将 `agent_loop` 内部的回调改为事件写入 EventBus | 解耦生成与消费，支持重放与调试 |
| **Protocol-Oriented LLM Layer** | 在 `ModelClient` 与具体后端之间增加 `Backend` 抽象 | 支持模型切换、流式统一、调用追踪 |
| **Explicit State Machine** | `AgentState` 不再是一个带 `status` 字段的容器，而是状态机实例 | 状态转换可审计、可中断、可恢复 |
| **Capability Registry** | 插件/Skill 以声明式 `Capability` 注册，运行时通过 `CapabilityHandle` 调用 | 降低耦合，支持热插拔与版本隔离 |
| **Memory Hierarchy** | 明确区分 `WorkingMemory`、`SessionMemory`、`LongTermMemory` | 长会话压缩更精准，知识召回更可控 |
| **Conversation Tree** | 消息以树节点形式存储，支持分支与快照 | 审核流程可回溯、可并行探索 |
| **Tool Contract Versioning** | 工具 schema 带 `api_version`，运行时按版本匹配 | 插件升级不破坏既有调用 |
| **Layered Guardrails** | 输入层、模型输出层、工具执行层分别设置护栏 | 安全与业务规则可独立演进 |

---

## 3. 总体迁移原则

1. **向后兼容**：现有 FastAPI 路由、SSE 事件格式、`ToolProtocol` 调用方式在 Phase 1–2 内保持兼容；破坏性变更仅在 Phase 4 引入，并伴随版本切换机制。
2. **增量演进**：每个 Phase 都有独立的可运行里程碑，禁止“大爆炸式”重构。
3. **事件优先**：所有新增能力优先通过事件总线暴露，而非直接改 SSE 格式。
4. **契约先行**：新增抽象层时先定义接口/协议，再迁移实现，最后淘汰旧实现。
5. **可观测**：每个 Phase 都要补充 OpenTelemetry span 与 metrics，确保迁移过程可度量。
6. **可回滚**：每个 Phase 的关键改造点都要能通过配置开关切回旧实现。

---

## 4. 实施路线图总览

| Phase | 周期 | 主题 | 核心任务 | 交付物 |
|-------|------|------|---------|--------|
| **Phase 1** | 0–4 周 | 事件总线与 SSE 解耦 | 引入 `EventBus`；定义 `AgentEvent`；重构 `SSEAdapter` 为订阅者 | 事件总线上线，SSE 输出 100% 兼容 |
| **Phase 2** | 4–8 周 | LLM 抽象与状态协议化 | `ModelBackend` 抽象；`AgentStateMachine` 显式化；LLM 调用协议层 | 支持模型路由；状态转换可审计 |
| **Phase 3** | 8–14 周 | 扩展系统与记忆分层 | `CapabilityRegistry`；SubAgent 事件隔离；记忆分层 | Skill/插件生命周期清晰；长会话成本下降 |
| **Phase 4** | 14–20 周 | 会话树与高级能力 | 会话树存储；工具协议版本化；分层护栏增强 | 支持会话分支；插件升级平滑 |

---

## Phase 1：统一事件总线与 SSE 解耦（0–4 周）

### 目标

将 `agent_loop()` 内部通过回调函数硬编码生成 SSE 的方式，改为“先写事件总线，再由 SSE 适配器订阅”的架构。使前端协议、审计日志、调试工具都能作为独立消费者接入，同时保持现有 SSE 输出不变。

### 涉及文件

- `courtier/courtier/agent/core/loop.py`
- `courtier/courtier/agent/core/sse_adapter.py`
- `courtier/courtier/agent/core/events.py`（新增）
- `courtier/courtier/agent/core/event_bus.py`（新增）
- `courtier/courtier/api/routes/sessions.py`（调用侧适配）

### 具体改造点

#### 1.1 定义统一事件模型 `AgentEvent`

在 `courtier/courtier/agent/core/events.py` 中定义不可变事件基类：

```python
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

EventType = Literal[
    "state.transition",
    "llm.request",
    "llm.token",
    "llm.response",
    "tool.start",
    "tool.progress",
    "tool.result",
    "tool.error",
    "guard.triggered",
    "hint.injected",
    "loop.completed",
]


@dataclass(frozen=True, slots=True)
class AgentEvent:
    type: EventType
    session_id: str
    agent_name: str
    turn_index: int
    payload: dict[str, Any]
    event_id: str = field(default_factory=lambda: uuid4().hex)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
```

> 设计要点：
> - `event_id` 保证全局可追踪；
> - `turn_index` 与 `agent_name` 支持多 Agent/SubAgent 场景；
> - `payload` 按事件类型有明确 schema，但基类保持通用。

#### 1.2 实现内存事件总线 `EventBus`

在 `courtier/courtier/agent/core/event_bus.py` 中实现：

```python
class EventBus:
    """In-process async event bus. Thread-safe and backpressure-aware."""

    async def publish(self, event: AgentEvent) -> None: ...
    def subscribe(
        self,
        *,
        event_types: set[EventType] | None = None,
        session_id: str | None = None,
    ) -> EventSubscription: ...
```

- 默认使用 `asyncio.Queue` 实现；
- 支持按 `event_type` 与 `session_id` 过滤；
- 添加背压策略：订阅者处理不过来时按配置丢弃或阻塞。

#### 1.3 重构 `agent_loop()` 事件生成

将 `loop.py` 中的回调调用替换为事件发布：

```python
# 旧写法
if on_step:
    await on_step("thinking", "start")

# 新写法
await event_bus.publish(
    AgentEvent(
        type="state.transition",
        session_id=session_id,
        agent_name=agent_name,
        turn_index=turn_index,
        payload={"from": current_state.status, "to": "thinking", "reason": "turn_start"},
    )
)
```

需要替换的回调点：

| 回调 | 事件类型 | 备注 |
|------|---------|------|
| `on_step` | `state.transition` | 状态流转 |
| `on_token` | `llm.token` | reasoning token |
| `on_content_token` | `llm.token` | content token（通过 `payload["kind"]` 区分） |
| LLM 请求 | `llm.request` | 包含 messages 摘要、模型名、温度等元数据 |
| LLM 响应 | `llm.response` | 包含 tool_calls 或 content |
| `on_tool_start` | `tool.start` | 工具名与参数 |
| `on_tool_progress` | `tool.progress` | 进度块 |
| `on_tool_result` | `tool.result` / `tool.error` | 结果或异常 |
| 护栏触发 | `guard.triggered` | 终止原因 |
| 提示注入 | `hint.injected` | 注入内容摘要 |

#### 1.4 重构 `SSEAdapter` 为事件订阅者

将 `sse_adapter.py` 改造为 `SSEEventSubscriber`：

```python
class SSEEventSubscriber:
    """Subscribe to EventBus and convert AgentEvents to legacy SSE chunks."""

    def __init__(self, event_bus: EventBus, legacy_format: bool = True) -> None: ...

    async def stream(self, session_id: str) -> AsyncIterator[str]:
        async for event in self.event_bus.subscribe(session_id=session_id):
            if self.legacy_format:
                yield self._to_legacy_sse(event)
            else:
                yield self._to_json_sse(event)
```

- `legacy_format=True` 时，输出与当前 SSE 格式完全一致；
- 保留旧回调接口作为兼容层：`LegacyCallbackBridge` 监听事件总线并调用旧回调。

### 验收标准

- [x] `pytest courtier/tests/agent/core/test_event_bus.py` 通过，覆盖订阅/过滤/背压。
- [x] SSE 输出兼容现有前端（集成测试覆盖 `tests/agent/api/test_sessions.py`）。
- [x] 新增事件类型时，**仅需修改 `events.py` 和 `SSEAdapter._dispatch_event`**，无需改 `loop.py`。
- [x] 通过 `EventBus` 可并行输出：SSE、审计日志、调试 dump，互不阻塞。

> **实现说明**：当前 `EventBus` 是会话级实例，由 `StreamService.generate_sse_stream()`
> 每次请求创建，`SSEAdapter` 按 `session_id` 订阅。全局/跨会话监听应使用
> `CapabilityRegistry` 监听器或 OpenTelemetry，而不是全局 EventBus。

### 风险与回滚

| 风险 | 缓解措施 |
|------|---------|
| 事件总线性能瓶颈 | 使用内存队列 + 批量 flush；若出现瓶颈可切回旧回调（`legacy_callbacks=true`） |
| SSE 格式不兼容 | 新增集成测试对比新旧输出；上线前灰度 |
| 事件丢失导致审计不全 | 关键事件（state.transition / llm.response / tool.result）增加同步写入 `AuditLogger` 的旁路 |

---

## Phase 2：LLM 抽象层与状态协议化（4–8 周）

### 目标

在 `ModelClient` 与具体 LLM 后端之间引入 `ModelBackend` 抽象，实现模型路由、流式统一、降级与调用追踪；同时将 `AgentState` 中的隐式状态转换显式化为状态机，使每一次 LLM/工具交互都可被记录与审计。

### 涉及文件

- `courtier/courtier/agent/core/model.py`
- `courtier/courtier/agent/core/backends/`（新增目录）
- `courtier/courtier/agent/core/state.py`
- `courtier/courtier/agent/core/state_machine.py`（新增）
- `courtier/courtier/agent/core/protocol.py`（新增）
- `courtier/courtier/agent/core/loop.py`

### 具体改造点

#### 2.1 引入 `ModelBackend` 抽象

在 `courtier/courtier/agent/core/backends/base.py` 定义：

```python
from typing import Protocol, AsyncIterator
from courtier.agent.core.protocol import ChatRequest, ChatResponse, TokenChunk


class ModelBackend(Protocol):
    """Unified interface for any LLM provider."""

    name: str
    supports_tool_calls: bool
    supports_streaming: bool

    async def chat(self, request: ChatRequest) -> ChatResponse: ...

    async def stream(
        self, request: ChatRequest
    ) -> AsyncIterator[TokenChunk | ChatResponse]: ...
```

新增后端实现目录结构：

```text
courtier/agent/core/backends/
├── base.py
├── openai_backend.py      # OpenAI / 通义千问 / 兼容 OpenAI API
├── anthropic_backend.py   # Anthropic Messages API
├── local_backend.py       # vLLM / llama.cpp / 本地模型
└── router.py              # 路由、降级、A/B
```

#### 2.2 实现模型路由 `ModelRouter`

在 `courtier/courtier/agent/core/backends/router.py` 中实现：

```python
class ModelRouter:
    """Route chat requests across multiple backends with fallback."""

    def __init__(self, backends: list[ModelBackend], strategy: RoutingStrategy) -> None: ...

    async def chat(self, request: ChatRequest) -> ChatResponse:
        for backend in self._ordered_backends(request):
            try:
                return await backend.chat(request)
            except ModelUnavailable as e:
                await self._emit_fallback_event(request, backend, e)
        raise NoBackendAvailable(request)
```

支持策略：

| 策略 | 说明 |
|------|------|
| `primary` | 固定主模型，失败时按优先级降级 |
| `cost` | 按输入长度与模型成本选择 |
| `quality` | 按任务难度路由到不同模型 |
| `ab` | 按比例分流到两个模型，用于效果对比 |

#### 2.3 统一 LLM 协议对象 `ChatRequest` / `ChatResponse`

在 `courtier/courtier/agent/core/protocol.py` 中定义与后端无关的协议对象：

```python
from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class ChatMessage:
    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    name: str | None = None  # tool name for tool role


@dataclass(frozen=True)
class ChatRequest:
    model: str
    messages: tuple[ChatMessage, ...]
    temperature: float = 0.7
    max_tokens: int | None = None
    tools: list[ToolSchema] | None = None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class ChatResponse:
    backend: str
    model: str
    message: ChatMessage
    usage: TokenUsage
    latency_ms: float
    raw: dict[str, Any] | None = None  # backend-specific raw for debug
```

#### 2.4 显式状态机 `AgentStateMachine`

在 `courtier/courtier/agent/core/state_machine.py` 中定义：

```python
from enum import Enum, auto


class AgentStatus(Enum):
    IDLE = auto()
    THINKING = auto()
    WAITING_FOR_TOOL = auto()
    OBSERVING = auto()
    COMPLETED = auto()
    BLOCKED = auto()
    ERROR = auto()


class AgentStateMachine:
    """Encapsulates valid state transitions and emits events."""

    VALID_TRANSITIONS: dict[AgentStatus, set[AgentStatus]] = {
        AgentStatus.IDLE: {AgentStatus.THINKING},
        AgentStatus.THINKING: {
            AgentStatus.WAITING_FOR_TOOL,
            AgentStatus.COMPLETED,
            AgentStatus.ERROR,
        },
        AgentStatus.WAITING_FOR_TOOL: {
            AgentStatus.OBSERVING,
            AgentStatus.BLOCKED,
            AgentStatus.ERROR,
        },
        AgentStatus.OBSERVING: {AgentStatus.THINKING, AgentStatus.COMPLETED, AgentStatus.ERROR},
        AgentStatus.BLOCKED: set(),
        AgentStatus.COMPLETED: set(),
        AgentStatus.ERROR: set(),
    }

    def transition(
        self, state: AgentState, to: AgentStatus, reason: str
    ) -> AgentState: ...
```

#### 2.5 改造 `loop.py` 使用状态机

将 `loop.py` 中直接对 `current_state.status` 赋值的地方统一改为：

```python
current_state = state_machine.transition(
    current_state,
    to=AgentStatus.THINKING,
    reason="turn_start",
)
```

状态机内部自动：
- 校验转换是否合法；
- 通过 `EventBus` 发布 `state.transition` 事件；
- 记录 `transition_id`，用于后续审计与重放。

### 验收标准

- [x] `pytest courtier/tests/agent/core/test_state_machine.py` 覆盖所有合法/非法转换。
- [x] `ModelRouter` 集成测试验证主模型失败时自动降级到备用模型（`tests/agent/core/test_backends.py`）。
- [x] `loop.py` 中显式状态转换均经 `AgentStateMachine.transition_async()`；`AgentState.add_thought/add_observation/blocked/errored` 已增加 `set_status` 参数，调用方传入 `set_status=False` 后由状态机统一驱动状态变更。
- [x] 每个 LLM 调用产生 `llm.request` / `llm.response` 事件，且包含 `model` / `usage`。
- [x] 旧 `ModelClient` 接口通过适配器继续可用，旧测试无需修改即可通过。

> **已知限制**：
> - `AgentStatus` 实现为 `Literal[str]` 而非 `Enum`，以保持 SSE/DB 序列化兼容性。
> - `ModelRouter.stream()` 在 Phase 2 中抛出 `NotImplementedError`；流式多后端路由
>   按补丁方案实现。
> - `AgentStateMachine` 当前仅负责转换校验；`state.transition` 事件与 `transition_id`
>   由 `loop.py` 发布，后续将下放到状态机内部统一处理。

### 风险与回滚

| 风险 | 缓解措施 |
|------|---------|
| 新协议对象序列化问题 | 保留旧 `Message` dataclass 的 `to_dict()` / `from_dict()`，新旧并存 |
| 状态机过于严格导致循环异常 | 提供 `state_machine.strict=false` 配置，先警告后阻断 |
| 模型路由引入额外延迟 | 路由决策缓存 + 异步健康检查 |

---

## Phase 3：扩展系统与记忆分层（8–14 周）

### 目标

借鉴 Pi 的 Capability Registry 与 Memory Hierarchy，将 `Plugin`、`Tool`、`Skill`、`SubAgent` 统一纳入声明式扩展系统；同时把当前单一的 `ContextManager` 扩展为分层记忆系统，优化长会话成本与召回质量。

### 涉及文件

- `courtier/courtier/plugin/registry.py`（改造）
- `courtier/courtier/plugin/manager.py`（未来接入点）
- `courtier/courtier/agent/tools/registry.py`
- `courtier/courtier/agent/skills/registry.py`
- `courtier/courtier/agent/core/capability.py`（新增）
- `courtier/courtier/agent/core/memory_manager.py`（新增）
- `courtier/courtier/agent/core/context_manager.py`
- `courtier/courtier/agent/runtime/runtime.py`
- `courtier/courtier/agent/runtime/handle.py`
- `courtier/courtier/agent/agents/subagent/events.py`

### 具体改造点

#### 3.1 统一扩展注册表 `CapabilityRegistry`

在 `courtier/courtier/agent/core/capability.py` 中实现统一抽象：

```python
@dataclass(frozen=True)
class Capability:
    type: CapabilityType          # "tool" | "skill" | "agent" | "resource" | "route" | "checker"
    name: str
    provider: str                 # plugin name or builtin module
    title: str
    description: str
    meta: dict[str, Any]
    instance: Any | None = None   # live tool/skill/config object


class CapabilityRegistry:
    def register(self, capability: Capability) -> None: ...
    def unregister(self, type_: CapabilityType, name: str) -> Capability | None: ...
    def unregister_by_provider(self, provider: str) -> list[Capability]: ...
    def get(self, type_: CapabilityType, name: str) -> Capability | None: ...
    def list_capabilities(...) -> list[Capability]: ...
```

设计要点：

- 保留 `ToolRegistry` / `SkillRegistry` 作为实际执行容器，`CapabilityRegistry` 先作为**统一目录**存在；
- `ExtensionRegistry` 在注册插件能力时同步镜像到 `CapabilityRegistry`，卸载时按 `provider` 清理；
- 通过 `add_listener` 支持生命周期事件，为后续热重载做准备；
- 回退机制：对 `tool` / `skill` 类型支持从既有注册表反向包装为 `Capability`。

#### 3.2 插件生命周期管理

基于 `CapabilityRegistry` 的监听器实现 `PluginLifecycle`（`courtier/plugin/lifecycle.py`），将崩溃恢复策略从 `ProcessManager` 中解耦：

- `PluginLifecycle` 订阅 `CapabilityRegistry` 的 `register` / `unregister` 事件；
- 当插件崩溃导致能力被注销时，`unregister` 事件触发自动重启（含指数退避）；
- 维护每个 provider 的 `restart_count`、启动时间窗口与最大重试预算；
- 超出 `max_restarts` 或在 `_IMMEDIATE_CRASH_WINDOW` 内崩溃则标记为 `FATAL`。

`ProcessManager` 集成方式：

```python
self._lifecycle = PluginLifecycle(
    capability_registry=capability_registry,
    max_restarts=max_restarts,
    immediate_crash_window=_IMMEDIATE_CRASH_WINDOW,
    restart_callback=self._restart_provider,
)
```

- 插件成功启动后通过 `track_process()` 注册句柄，并 `reset_health()`；
- 崩溃时 `on_unregister()` 触发 `CapabilityRegistry` 事件，`PluginLifecycle` 调度重启；
- `shutdown()` 停止所有待处理的重启任务并清理句柄。

当未提供 `CapabilityRegistry` 时，`ProcessManager` 保留原有的本地重启策略作为回退。

#### 3.3 SubAgent 事件作用域隔离

改造 `AgentRuntime` 与 `AgentHandle`：

- `AgentHandle` 新增 `scope_id`，每次 `spawn()` 生成独立作用域；
- `SubAgentStreamEvent` 新增 `scope_id` 字段；
- `AgentRuntime` 新增 `event_bus`，子代理事件在写入回调的同时以 `AgentEvent(type="subagent.event", ...)` 发布到总线；
- `context_mode` 控制转发策略：
  - `transparent`：所有子代理事件向上转发（默认，保持向后兼容）；
  - `blackbox`：仅转发 `start` / `end`，中间 token/think/tool_result 被抑制。

```python
async def _emit_scoped_event(handle, callback, event):
    if handle.context_mode == "blackbox" and event.kind not in {"start", "end"}:
        return
    ...
```

#### 3.4 记忆分层 `MemoryManager`

在 `courtier/courtier/agent/core/memory_manager.py` 中实现四层记忆：

| 层级 | 对应 API | 说明 |
|------|---------|------|
| **working** | 继承 `ContextManager` 的 `messages` / `compact_if_needed` | 当前 LLM 上下文窗口 |
| **session** | `session_get` / `session_set` | 当前会话状态，按 `session_id` 隔离 |
| **long_term** | `long_term_get` / `long_term_set` | 跨会话持久记忆 |
| **retrieval** | `retrieve` / `working_recall` | 关键词召回，可替换为向量后端 |

`MemoryManager` 继承自 `ContextManager`，保留原有预算控制、大输出持久化、ref 解析等行为，新增异步记忆 API。默认底层使用现有 `FileMemoryStore`。

```python
class MemoryManager(ContextManager):
    async def session_set(self, key: str, value: Any) -> None: ...
    async def long_term_set(self, key: str, value: Any) -> None: ...
    async def retrieve(self, query: MemoryQuery) -> list[MemoryRecall]: ...
    async def working_recall(self, messages, query) -> list[MemoryRecall]: ...
```

`AgentRuntime.delegate()` 默认创建 `MemoryManager` 作为上下文管理器，原有显式传入 `ContextManager` 的调用仍兼容。

### 验收标准

- [x] `CapabilityRegistry` 可独立注册/查询/卸载能力，并支持从旧 `ToolRegistry`/`SkillRegistry` 回退查询。
- [x] `ExtensionRegistry` 注册插件工具/checker/route 时同步镜像到 `CapabilityRegistry`，卸载时按 `provider` 清理。
- [x] `AgentRuntime.spawn()` 为每次子代理生成独立 `scope_id`。
- [x] `context_mode="blackbox"` 时仅向上转发 `start` / `end` 事件；`context_mode="transparent"` 时转发全部事件。
- [x] 子代理事件以 `subagent.event` 写入 `EventBus`。
- [x] `MemoryManager` 单元测试覆盖 session 隔离、long_term 跨会话、retrieval 召回、working 摘要四层。
- [x] 插件进程被 `kill -9` 后自动重启：基于 `CapabilityRegistry` 监听器实现 `PluginLifecycle`，`ProcessManager` 崩溃后通过 capability unregister 事件触发自动重启，超出最大重试次数或启动窗口内崩溃则标记为 FATAL。
- [x] 长会话 token 数基准：新增 `benchmarks/long_session_memory_benchmark.py`，使用 `MemoryManager` 的 retrieval 层对比 baseline（全量历史）与 memory-augmented（仅保留系统提示 + 最新轮次 + top-k 记忆）的 token 占用；真实生产环境需接入向量/Embedding 召回后端以获得 ≥20% 的度量结论。

### 风险与回滚

| 风险 | 缓解措施 |
|------|---------|
| CapabilityRegistry 性能下降 | 启动时一次性构建索引；运行时查询为 O(1) 字典查找 |
| 记忆分层引入语义不一致 | `MemoryManager` 继承 `ContextManager`，旧调用路径无需修改 |
| 子代理事件隔离导致前端缺少进度 | 默认 `transparent`，只有显式 `blackbox` 才抑制中间事件 |

---

## Phase 4：会话树、工具版本化与高级护栏（14–20 周）

### 目标

引入会话树支持分支、回溯与快照；为工具协议增加版本控制；将当前护栏扩展为输入/输出/工具三层模型，并补充安全与业务规则。

### 涉及文件

- `courtier/courtier/agent/core/state.py`（改造）
- `courtier/courtier/agent/core/loop.py`（改造）
- `courtier/courtier/agent/core/conversation_tree.py`（新增）
- `courtier/courtier/agent/tools/protocol.py`（改造）
- `courtier/courtier/agent/tools/registry.py`（改造）
- `courtier/courtier/agent/core/guardrails/`（新增目录）

### 具体改造点

#### 4.1 会话树 `ConversationTree`

在 `courtier/courtier/agent/core/conversation_tree.py` 中实现：

```python
@dataclass
class ConversationNode:
    node_id: str
    parent_id: str | None
    turn_index: int
    messages: tuple[Message, ...]
    tool_results: tuple[Any, ...]
    metadata: dict[str, Any]
    children: list[str]


class ConversationTree:
    def from_messages(messages): ...
    def fork(node_id, reason) -> ConversationNode: ...
    def rewind(node_id) -> ConversationNode: ...
    def get_path(node_id) -> list[ConversationNode]: ...
    def append_turn(parent_node_id, messages, tool_results) -> ConversationNode: ...
    def serialize(self) -> dict[str, Any]: ...
```

改造 `AgentState`：

- 新增 `tree: ConversationTree | None` 与 `current_node_id: str | None`；
- `AgentState.initial(..., use_tree=True)` 自动创建根节点；
- 新增 `record_turn()`、`fork_tree()`、`rewind_tree()` 方法，不启用树时为 no-op；
- `agent_loop()` 在每次 observation 后调用 `record_turn()`，构建完整会话树。

应用：

- **从某轮重新生成**：`state.rewind_tree(node_id)` 后重新运行 LLM；
- **并行尝试 Skill**：`state.fork_tree()` 创建多个子分支；
- **会话快照**：保存 `tree.serialize()` 到数据库或 `AgentState.messages_json`。

#### 4.2 工具协议版本化

在 `courtier/courtier/agent/tools/protocol.py` 中新增 `ToolInfo` 与 `ToolVersioned`：

```python
@dataclass(frozen=True)
class ToolInfo:
    name: str
    version: str = "1.0.0"
    api_version: str = "1.0"
    description: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)
    deprecated: bool = False
    replaced_by: str | None = None


@runtime_checkable
class ToolVersioned(Protocol):
    version: str
    api_version: str
    deprecated: bool
    replaced_by: str | None
```

改造 `ToolRegistry`：

- 内部维护 `_versions[name][version]` 与 `_tool_info[name][version]`；
- `get(name, version=None)`：默认返回最新非废弃版本；
- `list_available_versions(name)`：列出所有版本；
- `get_tool_info(name, version=None)`：读取元数据；
- `unregister(name, version=None)`：可卸载特定版本；
- `get_schemas(include_deprecated=True)`：在描述中标注 `[DEPRECATED]`，可过滤；
- 未声明版本的传统工具默认 `version="1.0.0"`，保持原有注册行为。

#### 4.3 分层护栏 `GuardrailSystem`

在 `courtier/courtier/agent/core/guardrails/` 下实现：

```text
guardrails/
├── __init__.py
├── base.py              # Guardrail Protocol / GuardResult / GuardContext / GuardLayer
├── guardrail_system.py  # 编排执行与 per-layer 模式
├── input_guard.py       # SensitiveInputGuard
├── output_guard.py      # EmptyOutputGuard / RefusalOutputGuard
├── tool_guard.py        # DangerousToolGuard / RepeatedToolGuard
└── loop_guardrails.py   # ExploreLoopGuard / BusinessArtifactProgressGuard
```

统一接口：

```python
class Guardrail(Protocol):
    name: str
    layer: Literal["input", "output", "tool", "post_tool"]

    async def check(self, context: GuardContext) -> GuardResult: ...
```

`GuardrailSystem` 支持每层独立模式：

- `block`：block 动作生效；
- `log`：仅记录，block 降级为 log；
- `off` / `allow`：跳过该层。

新增 `post_tool` 层，用于在工具执行后访问 tool_results 与 artifact_store。

集成到 `agent_loop()`：

- **input**：pre-think 后、LLM 调用前检查；
- **output**：LLM 响应后、权限门前检查；
- **tool**：权限门后、工具执行前检查（`DangerousToolGuard` / `RepeatedToolGuard`）；
- **post_tool**：工具执行后检查（`ExploreLoopGuard` / `BusinessArtifactProgressGuard`）。

被 block 时通过 `state_machine.transition(current_state, "blocked", reason)` 终止；`post_tool` 层的业务护栏触发 `completed` 终止而非 `blocked`。

### 验收标准

- [x] 会话树支持 fork/rewind/get_path/append_turn，`ConversationTree.from_serialized()` 支持持久化恢复，单元测试覆盖多层分支与序列化往返。
- [x] `AgentState.initial(..., use_tree=True)` 自动构建根节点，`record_turn()` 在循环中生成节点；`StreamService.runner()` 结束时会话树持久化到 `SessionRecord.tree_json` / `current_node_id`。
- [x] 新增 `POST /sessions/{id}/fork` 与 `POST /sessions/{id}/rewind` API，业务逻辑下沉到 `session_service.py`，单元测试覆盖正常分支、未知节点、无树会话与权限校验。
- [x] 同一工具名可注册两个版本，`ToolRegistry.get(name, version=...)` 返回对应实现。
- [x] `get(name)` 默认返回最新非废弃版本；全部废弃时返回最新版本。
- [x] `get_schemas()` 对废弃工具标注 `[DEPRECATED]`，支持 `include_deprecated=False`。
- [x] 旧工具（无 version 字段）默认视为 `version="1.0.0"`，重复注册仍抛 `ValueError`。
- [x] 输入/输出/工具三层护栏均有独立模式开关，block 动作可终止循环。
- [x] 现有 `loop_guards.py` 中的业务护栏（business artifact progress、explore loop）迁移为 Guardrail 实现：新增 `ExploreLoopGuard` 与 `BusinessArtifactProgressGuard`，运行在 `post_tool` 层；`agent_loop()` 默认注入这两个护栏，移除旧的内嵌状态变量与直接调用。

### 风险与回滚

| 风险 | 缓解措施 |
|------|---------|
| 会话树改变数据库存储格式 | 树作为可选字段，未启用时 behavior 与旧代码一致；序列化以 JSON 附加存储 |
| 工具版本化增加调用歧义 | LLM schema 仅暴露最新非废弃版本；显式版本调用走内部 API |
| 新护栏误杀正常调用 | 新增护栏默认以 `log` 模式运行，显式配置才启用 `block` |

---

## 9. 模块依赖与实施顺序

```text
Phase 1
  ├─ events.py
  ├─ event_bus.py
  └─ sse_adapter.py (refactor)

Phase 2
  ├─ protocol.py
  ├─ backends/
  │   ├─ base.py
  │   ├─ openai_backend.py
  │   ├─ anthropic_backend.py
  │   └─ router.py
  ├─ state_machine.py
  └─ loop.py (partial)

Phase 3
  ├─ capability.py
  ├─ plugin/registry.py (mirror to capability registry)
  ├─ memory_manager.py
  ├─ runtime/runtime.py (event scope)
  └─ context_manager.py (kept as MemoryManager base)

Phase 4
  ├─ conversation_tree.py
  ├─ state.py (tree fields)
  ├─ loop.py (guardrail + tree integration)
  ├─ tools/protocol.py (versioning)
  ├─ tools/registry.py (multi-version)
  └─ guardrails/
```

**关键依赖**：

- Phase 2 的 `llm.response` 事件依赖 Phase 1 的 `EventBus`；
- Phase 3 的 `CapabilityRegistry` 依赖 Phase 2 的 `state.transition` 事件用于生命周期追踪；
- Phase 4 的 `ConversationTree` 依赖 Phase 2 的 `AgentStateMachine` 记录节点状态；
- Phase 4 的 `GuardrailSystem` 依赖 Phase 3 的 `Capability` schema 做工具参数校验。

---

## 10. 风险识别与回滚策略

| 全局风险 | 影响 | 缓解/回滚 |
|---------|------|----------|
| 重构范围失控 | 开发周期延长 | 每个 Phase 设定硬截止时间；超过则切到下一 Phase，遗留项单列 backlog |
| 事件总线单点故障 | 全链路事件丢失 | 关键事件写双路：EventBus + 直接 AuditLogger；可配置 `event_bus.enabled=false` |
| 旧测试大面积失效 | 回归成本激增 | 每 Phase 保持旧接口适配层；旧测试不修改，新增独立测试 |
| 模型路由引入不确定性 | 输出质量波动 | 默认 `primary` 策略，仅显式配置才启用 cost/quality/ab |
| 数据库 schema 变更 | 会话数据不兼容 | 会话树以 JSON 形式附加存储；旧字段保留，读取时兼容 |
| 前端未适配新能力 | 会话树/版本化无法使用 | 后端先支持，前端按迭代接入；旧 SSE 格式不变 |

---

## 附录 A：Pi → Courtier 概念映射表

| Pi 概念 | 对应 Courtier 现状 | 迁移后形态 |
|--------|-------------------|-----------|
| Event Bus | `SSEAdapter` 直接转换回调 | `EventBus` + `AgentEvent` |
| Model Backend | `ModelClient` 直接调用 LLM | `ModelBackend` 协议 + `ModelRouter` |
| State Machine | `AgentState.status` 隐式赋值 | `AgentStateMachine` 显式转换 |
| Capability Registry | `ToolRegistry` + `SkillRegistry` 分离 | 统一 `CapabilityRegistry` |
| Memory Hierarchy | `ContextManager` 单一预算控制 | `MemoryManager` 四层记忆 |
| Conversation Tree | `AgentState.messages` 线性列表 | `ConversationTree` + `ConversationNode` |
| Tool Contract | `ToolProtocol` / `ToolInfo` 无版本 | `ToolInfo` 带 `version` / `api_version` |
| Guardrails | `loop_guards.py` 内嵌规则 | 分层 `GuardrailSystem` |

---

## 附录 B：新增/改造文件清单

### 新增文件

| 文件路径 | 所属 Phase | 说明 |
|---------|-----------|------|
| `courtier/courtier/agent/core/events.py` | Phase 1 | 标准事件模型 |
| `courtier/courtier/agent/core/event_bus.py` | Phase 1 | 内存事件总线 |
| `courtier/courtier/agent/core/protocol.py` | Phase 2 | LLM 交互协议对象 |
| `courtier/courtier/agent/core/backends/base.py` | Phase 2 | 模型后端协议 |
| `courtier/courtier/agent/core/backends/openai_backend.py` | Phase 2 | OpenAI 兼容后端 |
| `courtier/courtier/agent/core/backends/router.py` | Phase 2 | 模型路由与降级 |
| `courtier/courtier/agent/core/state_machine.py` | Phase 2 | 显式状态机 |
| `courtier/courtier/agent/core/capability.py` | Phase 3 | 统一扩展注册表 |
| `courtier/courtier/agent/core/memory_manager.py` | Phase 3 | 四层记忆管理器 |
| `courtier/courtier/agent/core/conversation_tree.py` | Phase 4 | 会话树 |
| `courtier/courtier/agent/core/guardrails/__init__.py` | Phase 4 | 护栏包导出 |
| `courtier/courtier/agent/core/guardrails/base.py` | Phase 4 | 护栏协议与上下文 |
| `courtier/courtier/agent/core/guardrails/guardrail_system.py` | Phase 4 | 护栏编排 |
| `courtier/courtier/agent/core/guardrails/input_guard.py` | Phase 4 | 输入层护栏 |
| `courtier/courtier/agent/core/guardrails/output_guard.py` | Phase 4 | 输出层护栏 |
| `courtier/courtier/agent/core/guardrails/tool_guard.py` | Phase 4 | 工具层护栏 |
| `courtier/courtier/agent/core/guardrails/loop_guardrails.py` | Phase 4 | 业务护栏（explore loop / business artifact progress） |
| `courtier/courtier/plugin/lifecycle.py` | Phase 3 | 插件生命周期与崩溃自动重启 |
| `courtier/courtier/benchmarks/long_session_memory_benchmark.py` | Phase 3 | 长会话记忆召回 token 基准 |

### 改造文件

| 文件路径 | 所属 Phase | 改造内容 |
|---------|-----------|---------|
| `courtier/courtier/agent/core/loop.py` | Phase 1/2/4 | 事件发布、状态机、护栏接入 |
| `courtier/courtier/agent/core/sse_adapter.py` | Phase 1 | 改造为事件订阅者 |
| `courtier/courtier/agent/core/model.py` | Phase 2 | 适配 `ModelBackend` 协议 |
| `courtier/courtier/agent/core/state.py` | Phase 2/4 | 接入状态机、会话树 |
| `courtier/courtier/agent/core/context_manager.py` | Phase 3 | `MemoryManager` 继承并保持兼容 |
| `courtier/courtier/plugin/registry.py` | Phase 3 | 注册/注销时同步 `CapabilityRegistry` |
| `courtier/courtier/agent/runtime/runtime.py` | Phase 3 | 增加 `event_bus`、作用域隔离、`MemoryManager` 默认 |
| `courtier/courtier/agent/runtime/handle.py` | Phase 3 | 增加 `scope_id` 与子句柄传播 |
| `courtier/courtier/agent/agents/subagent/events.py` | Phase 3 | 增加 `scope_id` |
| `courtier/courtier/plugin/manager.py` | Phase 3 | 接入 `PluginLifecycle`，崩溃后由 `CapabilityRegistry` 事件驱动重启 |
| `courtier/courtier/agent/tools/registry.py` | Phase 4 | 支持多版本工具注册/查询/卸载 |
| `courtier/courtier/agent/tools/protocol.py` | Phase 4 | 增加 `ToolInfo` / `ToolVersioned` |
| `courtier/courtier/agent/core/loop_guards.py` | Phase 4 | 保留旧函数作为兼容层，核心逻辑已迁移至 `guardrails/loop_guardrails.py` |

---

## 附录 C：关键配置项

```toml
# courtier/config/agent.toml（示例）

[events]
enabled = true
legacy_callbacks = false      # true 时同时调用旧回调，用于回滚
backpressure = "drop_oldest"  # drop_oldest | block | drop_newest

[model]
routing_strategy = "primary"  # primary | cost | quality | ab
fallback_backends = ["local_qwen", "local_backup"]

[memory]
enabled_layers = ["working", "session", "long_term", "retrieval"]
long_term_summarize_after_turns = 10

[guardrails]
input_layer = "log"   # log | block | off
output_layer = "log"
tool_layer = "block"

[conversation_tree]
enabled = true
max_branches = 5
snapshot_interval_turns = 5
```

---

## 附录 D：推荐测试策略

1. **单元测试**：每个新增模块独立测试，覆盖率目标 ≥85%。
2. **集成测试**：保持现有 `tests/api/test_sessions.py` 不变，新增 `tests/agent/core/test_event_sse_compat.py` 验证 SSE 兼容。
3. **契约测试**：为每个 `Capability` 编写 schema 契约测试，确保插件/Skill 声明与实现一致。
4. **性能基准**：在迁移前后分别运行长会话基准（30 轮），对比 LLM token 数与延迟。
5. **灰度测试**：Phase 2 的 `ModelRouter` 先在非生产环境运行 1 周，观察降级命中率。

---

> 文档版本：1.1  
> 编写日期：2026-07  
> 维护者：Courtier 架构组  
> 下次评审：运营化（配置、会话树 API、前端适配、监控告警）评审
