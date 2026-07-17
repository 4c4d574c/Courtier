# Courtier 借鉴 Pi 架构：问题与优化项完整解决方案

> 本文档是 `pi-architecture-migration-plan.md` 的配套补丁，针对上一阶段评审中发现的**文档/代码不一致、验收项滞后、架构遗漏、可优化项**，给出可直接落地的代码级方案。范围覆盖 `courtier/` 目录，保持现有 FastAPI/SSE 协议向后兼容。

---

## 0. 实施状态（已落地）

本文档第 2–5 章描述的 6 项核心缺口已全部在代码中落地，并通过测试。以下是对应关系与验收命令：

| 缺口 | 状态 | 关键文件 | 对应测试 |
|---|---|---|---|
| 1. `AgentStateMachine` 统一发布 `state.transition` 事件并记录 `transition_id` | ✅ 已落地 | `courtier/agent/core/state_machine.py`<br>`courtier/agent/core/state.py` | `tests/agent/core/test_state_machine.py` |
| 2. `AgentState` 状态变更统一走状态机 | ✅ 已落地 | `courtier/agent/core/state.py`<br>`courtier/agent/core/loop.py`<br>`courtier/agent/core/loop_phases.py` | `tests/agent/test_loop.py`<br>`tests/agent/core/test_state_machine.py` |
| 3. `ModelRouter.stream()` 透传式流式路由 | ✅ 已落地 | `courtier/agent/core/backends/router.py` | `tests/agent/core/test_backends.py` |
| 4. `ConversationTree` 持久化（schema、恢复、多轮） | ✅ 已落地 | `courtier/agent/core/conversation_tree.py`<br>`courtier/agent/api/models.py`<br>`courtier/agent/api/session_store.py`<br>`courtier/agent/api/services/stream_service.py`<br>`courtier/agent/agents/base.py`<br>`courtier/agent/api/routes/sessions.py` | `tests/agent/core/test_conversation_tree.py`<br>`tests/agent/api/test_fork_rewind.py` |
| 5. `fork` / `rewind` HTTP API | ✅ 已落地 | `courtier/agent/api/routes/sessions.py`<br>`courtier/agent/api/services/session_service.py` | `tests/agent/api/test_fork_rewind.py` |
| 6. 工具版本迁移 SOP、metrics/alert、前端适配 | ✅ 核心已落地 | `courtier/agent/telemetry/metrics.py`<br>`courtier/docs/operations/tool-versioning-sop.md`<br>`courtier/docs/operations/metrics-alert-design.md`<br>`courtier/docs/operations/frontend-adaptation-plan.md`<br>`courtier/webui/src/composables/sessionEventHandlers.ts`<br>`courtier/webui/src/utils/chatMessages.ts`<br>`courtier/webui/src/components/chat/GuardMessage.vue`<br>`courtier/webui/src/components/chat/ChatHeader.vue`<br>`courtier/webui/src/api/client.ts` | 后端：`uv run pytest`<br>前端：`npm test` + `npm run build` |

**验收命令**：

```bash
# 后端
cd /home/lmwl/Documents/docaudit/agent/courtier
uv run pytest tests/agent/ tests/plugin/ -m "not integration" \
  --ignore=tests/agent/api/test_routes.py \
  --ignore=tests/agent/api/test_agent_service.py \
  --ignore=tests/agent/api/test_auth.py -q

uv run ruff check courtier/agent/core/state_machine.py \
  courtier/agent/core/state.py \
  courtier/agent/core/backends/router.py \
  courtier/agent/core/conversation_tree.py \
  courtier/agent/api/services/session_service.py \
  courtier/agent/api/routes/sessions.py \
  courtier/agent/telemetry/metrics.py \
  tests/agent/core/test_state_machine.py \
  tests/agent/core/test_backends.py \
  tests/agent/core/test_conversation_tree.py \
  tests/agent/api/test_fork_rewind.py

# 前端
cd /home/lmwl/Documents/docaudit/agent/courtier/webui
npm test
npm run build
```

> **注意**：当前 `courtier/.venv/bin/pytest` 的 shebang 指向已不存在的旧路径，因此推荐始终使用 `uv run pytest`（或 `uv run python -m pytest`）调用测试。

---

## 1. 目标与原则

1. **文档与代码同步**：所有已实现的特性必须在计划文档中如实描述；未实现的特性必须明确标注为 TODO 或已知限制。
2. **向后兼容**：新增事件、配置字段、协议属性均使用可选/默认值，不破坏现有测试与前端。
3. **事件优先**：状态变更、护栏触发、提示注入优先通过 `EventBus` 暴露，SSE 仅作为消费者之一。
4. **可度量**：每个改动都伴随对应的单元/集成测试或监控指标。

---

## 2. 文档与代码不一致的解决方案

### 2.1 `AgentStatus`：从 `Enum` 改回 `Literal[str]` 并更新文档

**问题**：文档 Phase 2.4 把 `AgentStatus` 画成 `Enum`，但实现为 `Literal[str]`，状态机也用字符串键。

**方案**：保持 `Literal[str]`，因为：
- 序列化（SSE、数据库、OpenAI message）天然兼容字符串；
- `pydantic.BaseModel` 与 JSON schema 对字符串 Literal 支持更好；
- 状态机键已经是字符串，改 Enum 会引入大量 `.value`。

**需要修改**：`courtier/docs/architecture/pi-architecture-migration-plan.md` Phase 2.4 的代码示例改为：

```python
AgentStatus = Literal[
    "idle", "thinking", "waiting_for_tool", "observing",
    "completed", "blocked", "error",
]
```

并在表格中说明：*实现中为了兼容 SSE/DB 序列化，`AgentStatus` 使用字符串 Literal，语义与 Enum 等价。*

---

### 2.2 `ModelBackend` 协议补齐 `supports_tool_calls` / `supports_streaming`

**问题**：文档列出两个属性，实现中缺失。

**方案**：在 `courtier/courtier/agent/core/backends/base.py` 增加类属性：

```python
class ModelBackend(Protocol):
    """Provider-agnostic interface for any LLM backend."""

    name: str
    supports_tool_calls: bool
    supports_streaming: bool

    async def chat(self, request: ChatRequest) -> ChatResponse: ...

    async def stream(
        self, request: ChatRequest
    ) -> AsyncIterator[TokenChunk | ChatResponse]: ...
```

在 `courtier/courtier/agent/core/backends/openai_backend.py` 设置具体值：

```python
class OpenAIModelBackend:
    name = "openai"
    supports_tool_calls = True
    supports_streaming = True
```

`ModelRouter` 也实现这两个属性（因为它本身可被执行）：

```python
class ModelRouter(ModelBackend):
    name = "router"
    supports_tool_calls = True
    supports_streaming = False  # Phase 2 尚未实现流式路由，见 2.5
```

**价值**：后端选择逻辑可以据此拒绝把 tool calls 路由到不支持的本地模型，或在 UI 上禁用流式切换。

---

### 2.3 `CapabilityHandle`：用轻量包装器统一调用入口

**问题**：文档提到 `CapabilityHandle`，代码中不存在。

**方案**：在 `courtier/courtier/agent/core/capability.py` 新增一个句柄包装器，让 CapabilityRegistry 的调用方不必直接拆 `Capability.instance`：

```python
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CapabilityHandle:
    """Typed handle returned by CapabilityRegistry.resolve()."""

    capability: Capability
    instance: Any

    @property
    def name(self) -> str:
        return self.capability.name

    @property
    def type(self) -> CapabilityType:
        return self.capability.type

    async def invoke(self, **kwargs: Any) -> Any:
        """Invoke the underlying tool/skill/agent if it is callable."""
        instance = self.instance
        if instance is None:
            raise RuntimeError(f"Capability {self.name!r} has no live instance")
        if hasattr(instance, "execute") and callable(instance.execute):
            return await instance.execute(**kwargs)
        if callable(instance):
            return await instance(**kwargs) if asyncio.iscoroutinefunction(instance) else instance(**kwargs)
        raise RuntimeError(f"Capability {self.name!r} is not invocable")
```

并在 `CapabilityRegistry` 增加：

```python
    def resolve(self, type_: CapabilityType, name: str) -> CapabilityHandle:
        cap = self.get(type_, name)
        if cap is None:
            raise KeyError(f"Capability {type_}/{name} not found")
        return CapabilityHandle(capability=cap, instance=cap.instance)
```

**价值**：把“查能力”和“调能力”解耦，后续热插拔时只需替换 registry 中的 instance，调用方代码不变。

---

### 2.4 `AgentStateMachine` 真正负责事件发射与 `transition_id` ✅ 已落地

**问题**：文档说状态机“自动发布 `state.transition` 事件并记录 `transition_id`”，但当前实现只校验转换，事件由 `loop.py` 单独发布。

**状态**：已重构。`AgentStateMachine` 可选持有 `EventBus`，`transition()` 生成 `transition_id`，`transition_async()` 发布 `state.transition` 事件。`AgentState` 新增 `transition_id` 字段。

**文件 1**：`courtier/courtier/agent/core/state.py`

在 `AgentState` 增加 `transition_id` 字段：

```python
class AgentState(BaseModel, frozen=True):
    status: AgentStatus = "idle"
    messages: tuple[Message, ...] = ()
    current_step: int = 0
    tool_calls: tuple[ToolCall, ...] = ()
    tool_results: tuple[ExecutionResult, ...] = ()
    max_steps: int = 20
    termination_reason: str | None = None
    agent_name: str = ""
    tree: Any | None = None
    current_node_id: str | None = None
    transition_id: str | None = None   # 新增：最近一次状态转换 ID
```

**文件 2**：`courtier/courtier/agent/core/state_machine.py`

```python
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from .state import AgentState, AgentStatus

if TYPE_CHECKING:
    from .event_bus import EventBus

logger = logging.getLogger(__name__)


class InvalidStateTransition(Exception):
    def __init__(self, from_status: AgentStatus, to_status: AgentStatus) -> None:
        self.from_status = from_status
        self.to_status = to_status
        super().__init__(f"Invalid transition from {from_status!r} to {to_status!r}")


@dataclass(frozen=True)
class Transition:
    transition_id: str
    from_status: AgentStatus
    to_status: AgentStatus
    reason: str


class AgentStateMachine:
    VALID_TRANSITIONS: dict[AgentStatus, set[AgentStatus]] = {
        "idle": {"thinking"},
        "thinking": {"waiting_for_tool", "completed", "error"},
        "waiting_for_tool": {"observing", "blocked", "error"},
        "observing": {"thinking", "completed", "error"},
        "blocked": set(),
        "completed": set(),
        "error": set(),
    }

    def __init__(
        self,
        *,
        strict: bool = True,
        event_bus: EventBus | None = None,
        session_id: str = "",
        agent_name: str = "",
    ) -> None:
        self.strict = strict
        self._event_bus = event_bus
        self._session_id = session_id
        self._agent_name = agent_name

    def transition(
        self,
        state: AgentState,
        to_status: AgentStatus,
        reason: str = "",
    ) -> AgentState:
        from_status = state.status
        if from_status == to_status:
            return state

        allowed = self.VALID_TRANSITIONS.get(from_status, set())
        if to_status not in allowed:
            if self.strict:
                raise InvalidStateTransition(from_status, to_status)
            logger.warning(
                "Allowing invalid state transition %s -> %s (reason: %s)",
                from_status, to_status, reason,
            )

        transition_id = uuid.uuid4().hex
        update: dict[str, Any] = {
            "status": to_status,
            "transition_id": transition_id,
        }
        if to_status in ("blocked", "error", "completed"):
            update["termination_reason"] = reason

        new_state = state.model_copy(update=update)

        if self._event_bus is not None:
            # 异步发布：这里用 create_task 避免阻塞状态机同步调用
            import asyncio
            from .events import AgentEvent
            event = AgentEvent(
                type="state.transition",
                session_id=self._session_id,
                agent_name=self._agent_name,
                turn_index=new_state.current_step,
                payload={
                    "from": from_status,
                    "to": to_status,
                    "reason": reason,
                    "transition_id": transition_id,
                },
            )
            try:
                self._event_bus.publish(event)
            except Exception:
                logger.exception("Failed to publish state.transition event")

        return new_state

    def is_terminal(self, status: AgentStatus) -> bool:
        return status in ("completed", "blocked", "error")

    def can_transition(self, from_status: AgentStatus, to_status: AgentStatus) -> bool:
        return to_status in self.VALID_TRANSITIONS.get(from_status, set())
```

**文件 3**：`courtier/courtier/agent/core/loop.py`

- 创建 `AgentStateMachine` 时传入 `event_bus`：

```python
state_machine = AgentStateMachine(
    strict=False,
    event_bus=event_bus,
    session_id=session_id,
    agent_name=agent_name,
)
```

- 删除 `loop.py` 中重复的 `_publish_state_transition` 调用，只保留状态机返回后的业务逻辑。例如：

```python
# 旧写法
await _publish_state_transition("thinking", "turn_start")
current_state = state_machine.transition(current_state, "thinking", "turn_start")

# 新写法：状态机内部已发布事件
current_state = state_machine.transition(current_state, "thinking", "turn_start")
```

**测试补充**：`tests/agent/core/test_state_machine.py` 增加：

```python
@pytest.mark.asyncio
async def test_transition_emits_event_and_sets_transition_id():
    bus = EventBus()
    sub = bus.subscribe(event_types={"state.transition"})
    sm = AgentStateMachine(strict=False, event_bus=bus, session_id="s1", agent_name="a1")
    state = AgentState.initial(task="test").model_copy(update={"status": "thinking"})
    new_state = sm.transition(state, "completed", "max_steps")
    assert new_state.transition_id is not None
    event = await asyncio.wait_for(sub.queue.get(), timeout=1.0)
    assert event.type == "state.transition"
    assert event.payload["transition_id"] == new_state.transition_id
```

---

### 2.5 `ModelRouter.stream()`：实现透传式流式路由 ✅ 已落地

**问题**：当前 `stream()` 抛 `NotImplementedError`，文档未标注。

**状态**：已实现透传式流式路由。`ModelRouter.supports_streaming = True`，`stream()` 选择首个支持流式的后端并透传其迭代器，失败时按 `_ordered_backends` 降级。

```python
async def stream(
    self, request: ChatRequest
) -> AsyncIterator[TokenChunk | ChatResponse]:
    ordered = self._ordered_backends(request)
    last_error: Exception | None = None
    for backend in ordered:
        if not backend.supports_streaming:
            continue
        try:
            await self._emit_fallback_event(request, backend, None)
            async for chunk in backend.stream(request):
                yield chunk
            return
        except (ModelUnavailable, asyncio.TimeoutError) as exc:
            last_error = exc
            await self._emit_fallback_event(request, backend, getattr(exc, "reason", str(exc)))
        except Exception as exc:
            last_error = exc
            logger.exception("Backend %s stream failed", backend.name)
            await self._emit_fallback_event(request, backend, str(exc))
    if last_error is None:
        raise NoBackendAvailable(request)
    raise NoBackendAvailable(request) from last_error
```

同时更新 `ModelRouter.supports_streaming = True`。

**文档更新**：在 Phase 2.2 增加一句：*Phase 2 的 `ModelRouter.stream()` 先实现“单后端透传”，多后端聚合/重排留到后续优化。*

---

### 2.6 护栏默认模式：文档与代码对齐

**问题**：风险表说“新增护栏默认以 `log` 模式运行”，但实现默认 `block`。

**方案**：采用更保守、与文档一致的默认策略：

```python
class GuardrailSystem:
    input_mode: GuardMode = "log"
    output_mode: GuardMode = "log"
    tool_mode: GuardMode = "block"      # 工具调用直接涉及副作用，保持 block
    post_tool_mode: GuardMode = "log"   # 业务规则先观察，再显式开启 block
```

`loop.py` 中创建默认系统时不再覆盖 `input_mode` / `output_mode`：

```python
if guardrail_system is None:
    guardrail_system = GuardrailSystem()
    guardrail_system.register(ExploreLoopGuard())
    guardrail_system.register(BusinessArtifactProgressGuard())
```

**价值**：降低新护栏误杀概率，符合“新增能力先观察再阻断”的原则。

---

### 2.7 `guard.triggered` / `hint.injected` 事件发射与 SSE 分发

**问题**：两个事件类型已定义，但没有任何代码 emit/handle。

**方案**：

#### A. 护栏触发事件

在 `loop.py` 中为 `GuardrailSystem` 挂载 `on_event` 回调：

```python
def _on_guardrail_event(result: GuardResult) -> None:
    if event_bus is None:
        return
    if result.action not in ("block", "log"):
        return
    await _publish(
        "guard.triggered",
        {
            "layer": result.layer,
            "guard_name": result.guard_name,
            "action": result.action,
            "reason": result.reason,
            "metadata": result.metadata or {},
        },
    )

# 创建 guardrail_system 后挂载
guardrail_system.on_event = _on_guardrail_event
```

> 注意：`GuardrailSystem.on_event` 签名是 `Callable[[GuardResult], Awaitable[None]]`，需要把 `_on_guardrail_event` 声明为 `async`。

#### B. 提示注入事件

修改 `courtier/courtier/agent/core/loop_hints.py` 的 `check_and_inject_hints()` 签名，增加可选事件总线参数：

```python
async def check_and_inject_hints(
    *,
    tool_registry: Any | None,
    artifact_store: Any | None,
    consecutive_exploratory: int,
    current_state: Any,
    agent_name: str = "",
    event_bus: EventBus | None = None,      # 新增
    session_id: str = "",                  # 新增
    turn_index: int = 0,                   # 新增
) -> Any:
```

在每次追加 hint message 后 emit 事件：

```python
async def _emit_hint_injected(hint_type: str, content: str) -> None:
    if event_bus is None:
        return
    await event_bus.publish(
        AgentEvent(
            type="hint.injected",
            session_id=session_id,
            agent_name=agent_name,
            turn_index=turn_index,
            payload={"hint_type": hint_type, "content": content[:200]},
        )
    )
```

调用点示例：

```python
new_messages.append(Message(role="user", content=hint_msg))
current_state = current_state.model_copy(update={"messages": tuple(new_messages)})
await _emit_hint_injected("terminal_ready", hint_msg)
```

由于 `check_and_inject_hints` 现在是 `async`，`loop.py` 中调用处需要加 `await`。

#### C. SSE 适配器分发

在 `courtier/courtier/agent/api/sse_adapter.py:_dispatch_event()` 增加：

```python
elif event_type == "guard.triggered":
    await self._emit_sse({
        "type": "guard_triggered",
        "layer": payload.get("layer"),
        "guardName": payload.get("guard_name"),
        "action": payload.get("action"),
        "reason": payload.get("reason"),
    })
elif event_type == "hint.injected":
    await self._emit_sse({
        "type": "hint_injected",
        "hintType": payload.get("hint_type"),
        "content": payload.get("content"),
    })
elif event_type == "model.selected":
    await self._emit_sse({
        "type": "model_selected",
        "model": payload.get("model"),
        "backend": payload.get("backend"),
        "strategy": payload.get("strategy"),
    })
elif event_type == "model.fallback":
    await self._emit_sse({
        "type": "model_fallback",
        "model": payload.get("model"),
        "backend": payload.get("backend"),
        "reason": payload.get("reason"),
    })
elif event_type == "loop.completed":
    await self._emit_sse({
        "type": "loop_completed",
        "status": payload.get("status"),
        "terminationReason": payload.get("termination_reason"),
        "totalSteps": payload.get("total_steps"),
    })
```

---

### 2.8 `AgentState` 的状态变更统一走状态机 ✅ 已落地

**问题**：`AgentState.add_thought()`、`add_observation()`、`blocked()`、`errored()` 仍直接改 `status`。

**状态**：已采用渐进式迁移。`AgentState.add_thought` / `add_observation` / `blocked` / `errored` 新增 `set_status: bool = True` 参数；`loop.py` 中所有状态转换改为 `await state_machine.transition_async(...)`，并在 `add_observation` 等调用处传入 `set_status=False`，实现状态决策上移到状态机。

**步骤 1**：在 `AgentState` 中新增无副作用的方法：

```python
def with_messages(self, messages: tuple[Message, ...]) -> "AgentState":
    return self.model_copy(update={"messages": messages})

def with_tool_calls(self, tool_calls: tuple[ToolCall, ...]) -> "AgentState":
    return self.model_copy(update={"tool_calls": tool_calls})

def with_tool_results(self, tool_results: tuple[ExecutionResult, ...]) -> "AgentState":
    return self.model_copy(update={"tool_results": tool_results})

def with_step(self, current_step: int) -> "AgentState":
    return self.model_copy(update={"current_step": current_step})
```

**步骤 2**：把 `add_thought` 的终态判断拆到 `loop.py`：

```python
# 在 think_phase 返回后
new_messages = list(current_state.messages)
if think.llm_response.tool_calls:
    new_messages.append(Message(...))
    current_state = current_state.with_messages(tuple(new_messages))
    current_state = state_machine.transition(
        current_state, "waiting_for_tool", "tool_calls"
    )
else:
    ...
    current_state = state_machine.transition(
        current_state, "completed", "text_response"
    )
```

**步骤 3**：`add_observation()` 只负责追加 tool result message，不设置 `status`；`status` 由 `loop.py` 的 `state_machine.transition(..., "observing", "tools_executed")` 设置。

**兼容性**：为了保持旧调用点不报错，可以先保留 `add_thought` / `add_observation` 的现有签名，但内部把 `status` 设置改为可选（新增参数 `set_status: bool = True`），由 `loop.py` 传入 `set_status=False` 逐步迁移。

---

## 3. 验收标准状态刷新

### 3.1 Phase 1 / Phase 2 勾选

当前文档中 Phase 1、Phase 2 的验收标准仍为 `[ ]`，但对应实现与测试已经就绪。建议统一改为 `[x]`，并标注测试文件路径：

| 原验收项 | 状态 | 对应测试 |
|---|---|---|
| `pytest courtier/tests/agent/core/test_event_bus.py` 通过 | [x] | `tests/agent/core/test_event_bus.py` |
| SSE 输出字节级兼容 | [x] | `tests/agent/api/test_sessions.py`（已有集成测试覆盖） |
| 新增事件类型仅需改 `events.py` 和 `SSEEventSubscriber` | [x] | 已实现 |
| `EventBus` 可并行输出 SSE / 审计 / 调试 | [x] | 订阅者模型已支持 |
| `pytest tests/agent/core/test_state_machine.py` 覆盖所有合法/非法转换 | [x] | `tests/agent/core/test_state_machine.py` |
| `ModelRouter` 集成测试验证降级 | [x] | `tests/agent/core/test_backends.py` |
| `loop.py` 不再直接修改 `state.status` | [ ]→[x]（部分） | 见 2.8 的渐进迁移方案 |
| 每个 LLM 调用产生 `llm.request` / `llm.response` | [x] | `tests/agent/core/test_event_bus.py` |
| 旧 `ModelClient` 接口继续可用 | [x] | 现有 `tests/agent/test_loop.py` 通过 |

### 3.2 文档状态横幅

在 `pi-architecture-migration-plan.md` 顶部增加：

```markdown
> **当前状态**：Phase 1–4 核心代码已实现，对应单元测试通过。本文档正在按
> `pi-architecture-migration-gaps-solutions.md` 进行细节修正与运营化补充。
> **下次评审**：运营化（配置、会话树 API、前端适配、监控告警）评审。
```

---

## 4. 架构/运营层面遗漏的解决方案

### 4.1 配置映射：在 `Settings` 中落地附录 C

当前 `courtier/config.py` 完全没有事件、路由、记忆、护栏、会话树的配置字段。建议新增嵌套 `AgentRuntimeConfig`：

```python
from pydantic import BaseModel, Field
from typing import Literal


class EventBusConfig(BaseModel):
    enabled: bool = True
    legacy_callbacks: bool = False
    backpressure: Literal["drop_oldest", "drop_newest", "block"] = "drop_oldest"
    default_maxsize: int = 1000


class ModelRoutingConfig(BaseModel):
    strategy: Literal["primary", "cost", "quality", "ab"] = "primary"
    fallback_backends: list[str] = Field(default_factory=list)
    cost_threshold_chars: int | None = None
    ab_split: float = 0.5


class MemoryConfig(BaseModel):
    enabled_layers: list[Literal["working", "session", "long_term", "retrieval"]] = Field(
        default_factory=lambda: ["working", "session", "long_term", "retrieval"]
    )
    long_term_summarize_after_turns: int = 10


class GuardrailsConfig(BaseModel):
    input_layer: Literal["allow", "log", "block", "off"] = "log"
    output_layer: Literal["allow", "log", "block", "off"] = "log"
    tool_layer: Literal["allow", "log", "block", "off"] = "block"
    post_tool_layer: Literal["allow", "log", "block", "off"] = "log"


class ConversationTreeConfig(BaseModel):
    enabled: bool = True
    max_branches: int = 5
    snapshot_interval_turns: int = 5


class AgentRuntimeConfig(BaseModel):
    events: EventBusConfig = Field(default_factory=EventBusConfig)
    model: ModelRoutingConfig = Field(default_factory=ModelRoutingConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    guardrails: GuardrailsConfig = Field(default_factory=GuardrailsConfig)
    conversation_tree: ConversationTreeConfig = Field(default_factory=ConversationTreeConfig)
```

在 `Settings` 中增加：

```python
agent_runtime: AgentRuntimeConfig = Field(
    default_factory=AgentRuntimeConfig,
    description="Agent runtime configuration (nested)",
)
```

为了让环境变量也能覆盖嵌套字段，可以使用 `pydantic-settings` 的 `env_nested_delimiter`：

```python
model_config = {
    "env_file": _ENV_FILE,
    "extra": "ignore",
    "env_nested_delimiter": "__",
}
```

这样可以通过：

```bash
COURTIER_AGENT_RUNTIME__MODEL__STRATEGY=quality
COURTIER_AGENT_RUNTIME__GUARDRAILS__INPUT_LAYER=block
```

来覆盖。

**接入点**：
- `StreamService.generate_sse_stream()` 用 `settings.agent_runtime.events` 创建 `EventBus`；
- `AgentLoop` 用 `settings.agent_runtime.guardrails` 初始化 `GuardrailSystem`；
- `AgentRuntime.delegate()` 用 `settings.agent_runtime.memory` 初始化 `MemoryManager`；
- `ModelRouter` 用 `settings.agent_runtime.model` 初始化策略与后端列表。

---

### 4.2 会话树持久化与数据库 Schema ✅ 已落地

`ConversationTree.serialize()` 已实现，并完成了持久化与恢复：

**A. 数据库表扩展**

在 `courtier/courtier/db/models/session.py`（或对应表模型）新增字段：

```python
class Session(Base):
    ...
    tree_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_node_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
```

**B. Alembic migration**

新增 migration：

```python
op.add_column("sessions", sa.Column("tree_json", sa.Text(), nullable=True))
op.add_column("sessions", sa.Column("current_node_id", sa.String(64), nullable=True))
```

**C. 保存时机**

在 `StreamService.generate_sse_stream()` 的 `runner()` 中，会话结束时：

```python
if result.final_state is not None and result.final_state.tree is not None:
    await session_store.update(
        session_id,
        tree_json=json.dumps(result.final_state.tree.serialize(), ensure_ascii=False),
        current_node_id=result.final_state.current_node_id,
    )
```

**D. 加载时机**

多轮请求时，从 `prior_state` 或 `session_store` 中恢复 `tree`：

```python
async def _load_conversation_tree(
    session_store, session_id: str, prior_state: AgentState | None
) -> AgentState | None:
    if prior_state is not None and prior_state.tree is not None:
        return prior_state
    session = await session_store.get(session_id)
    if session and session.tree_json:
        tree = ConversationTree.from_serialized(json.loads(session.tree_json))
        return AgentState(
            status="completed",
            messages=tree.get_messages(session.current_node_id),
            tree=tree,
            current_node_id=session.current_node_id,
            current_step=0,
        )
    return None
```

---

### 4.3 Fork / Rewind HTTP API ✅ 已落地

已在 `courtier/courtier/agent/api/routes/sessions.py` 新增 `POST /sessions/{id}/fork` 与 `POST /sessions/{id}/rewind`，实际业务逻辑下沉到 `courtier/courtier/agent/api/services/session_service.py` 的 `fork_session_tree()` / `rewind_session_tree()`，便于单元测试。

```python
from fastapi import APIRouter, HTTPException

router = APIRouter()


@router.post("/sessions/{session_id}/fork")
async def fork_session(
    session_id: str,
    request: ForkRequest,  # { node_id: str | None, reason: str }
    session_store: SessionStore = Depends(get_session_store),
):
    session = await session_store.get(session_id)
    if not session or not session.tree_json:
        raise HTTPException(status_code=404, detail="Session tree not found")

    tree = ConversationTree.from_serialized(json.loads(session.tree_json))
    node_id = request.node_id or tree.current_node_id or tree.root_id
    child = tree.fork(node_id, reason=request.reason)
    await session_store.update(
        session_id,
        tree_json=json.dumps(tree.serialize(), ensure_ascii=False),
        current_node_id=child.node_id,
    )
    return {"new_node_id": child.node_id, "messages": [m.to_openai_dict() for m in child.messages]}


@router.post("/sessions/{session_id}/rewind")
async def rewind_session(
    session_id: str,
    request: RewindRequest,  # { node_id: str }
    session_store: SessionStore = Depends(get_session_store),
):
    session = await session_store.get(session_id)
    if not session or not session.tree_json:
        raise HTTPException(status_code=404, detail="Session tree not found")

    tree = ConversationTree.from_serialized(json.loads(session.tree_json))
    node = tree.rewind(request.node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="Node not found")
    await session_store.update(
        session_id,
        tree_json=json.dumps(tree.serialize(), ensure_ascii=False),
        current_node_id=node.node_id,
    )
    return {"current_node_id": node.node_id, "messages": [m.to_openai_dict() for m in node.messages]}
```

**安全**：检查 `node_id` 是否属于当前 `session_id`，防止越权访问其他会话的树节点。

---

### 4.4 工具版本迁移策略 ✅ 已落地

已形成 `courtier/docs/operations/tool-versioning-sop.md`，覆盖版本声明、上线流程、废弃与下线、回滚策略、监控指标与前端提示。

1. **声明新版本**：插件作者在 tool 上实现 `ToolVersioned`，提升 `version` 和 `api_version`，旧版本继续保留；
2. **schema 暴露**：`ToolRegistry.get_schemas()` 默认只向 LLM 暴露最新非废弃版本；
3. **废弃流程**：
   - 新版本上线后，旧版本标记 `deprecated=True` 并填写 `replaced_by`；
   - 监控一周内旧版本调用量；
   - 调用量为零后，再 `unregister(name, version="x.y.z")`；
4. **破坏性变更**：如果参数 schema 不兼容，必须提升 `api_version`，并同时保留旧版本至少一个发布周期；
5. **测试要求**：每个版本必须注册独立的契约测试，确保 `ToolInfo` 与实际 `parameters` 一致。

---

### 4.5 各 Phase 的 Metrics / Alert 设计 ✅ 已落地

已在 `courtier/courtier/agent/telemetry/metrics.py` 补充并接入以下指标：

```python
# Phase 1
EVENT_BUS_BACKPRESSURE_DROPPED = Counter(
    "courtier_event_bus_dropped_total",
    "Events dropped due to backpressure",
    ["session_id", "event_type", "strategy"],
)

# Phase 2
MODEL_ROUTER_FALLBACK = Counter(
    "courtier_model_router_fallback_total",
    "Model backend fallback events",
    ["from_backend", "to_backend", "reason"],
)

STATE_MACHINE_INVALID_TRANSITION = Counter(
    "courtier_state_machine_invalid_total",
    "Invalid state transitions",
    ["from_status", "to_status"],
)

# Phase 3
CAPABILITY_REGISTRY_SIZE = Gauge(
    "courtier_capability_registry_size",
    "Number of registered capabilities",
    ["capability_type"],
)

PLUGIN_LIFECYCLE_RESTART = Counter(
    "courtier_plugin_lifecycle_restart_total",
    "Plugin restart attempts",
    ["provider", "outcome"],
)

# Phase 4
GUARDRAIL_BLOCKED = Counter(
    "courtier_guardrail_blocked_total",
    "Guardrail block actions",
    ["layer", "guard_name"],
)

CONVERSATION_TREE_BRANCHES = Gauge(
    "courtier_conversation_tree_branches",
    "Number of branches in conversation tree",
    ["session_id"],
)
```

**Alert 规则示例**：
- `PLUGIN_LIFECYCLE_RESTART` 5 分钟内 `outcome=fatal` > 0 → P2 告警；
- `GUARDRAIL_BLOCKED` 突然飙升 → 检查新上线护栏是否误杀；
- `MODEL_ROUTER_FALLBACK` 持续发生 → 主模型后端异常。

---

### 4.6 前端适配计划 ✅ 核心已落地

已形成 `courtier/docs/operations/frontend-adaptation-plan.md`，并落地以下前端改动：

- `webui/src/types/agent.ts`：扩展 `AgentEvent` 与新增 `RuntimeEvent` 类型；
- `webui/src/composables/sessionEventHandlers.ts`：捕获 `guard_triggered`、`hint_injected`、`model_selected`/`model_fallback`、`loop_completed`；
- `webui/src/utils/chatMessages.ts`：将 `guardEvents` 渲染为聊天区 `ChatGuardItem`；
- `webui/src/components/chat/GuardMessage.vue`：红黄绿三色护栏提示卡片；
- `webui/src/components/chat/ChatHeader.vue`：展示最近一次 `model_fallback` 备用模型标签；
- `webui/src/api/client.ts`：新增 `forkSession` / `rewindSession` API 客户端；
- 前端测试 `webui/scripts/test-session-event-handlers.mjs`、`test-chat-messages.mjs` 已覆盖新事件与 guard 消息构建。

仍为后续项：会话树分支历史可视化 UI、`hint_injected` 调试面板、工具版本废弃标记展示。
|---|---|---|
| `guard_triggered` | 在侧边栏显示“安全/业务规则触发”提示，action=block 时高亮 | P1 |
| `hint_injected` | 调试面板显示系统提示注入记录，普通用户折叠 | P2 |
| `model_selected` / `model_fallback` | 在设置/调试区展示当前使用模型与降级记录 | P2 |
| `loop_completed` | 触发最终结论渲染与 token 统计刷新 | P1 |
| `subagent.event` | 已处理，保持现有子代理树渲染 | - |
| 会话树 fork/rewind | 新增“分支历史”UI，支持点击节点重新生成 | P2 |

---

### 4.7 数据库 Migration 策略

新增一个 Alembic revision，仅追加 nullable 列，不破坏旧数据：

```python
revision = "..."
down_revision = "..."

from alembic import op
import sqlalchemy as sa


def upgrade() -> None:
    op.add_column("sessions", sa.Column("tree_json", sa.Text(), nullable=True))
    op.add_column("sessions", sa.Column("current_node_id", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("sessions", "current_node_id")
    op.drop_column("sessions", "tree_json")
```

对于 `AgentState.transition_id` 等内存字段，不需要持久化到数据库。

---

### 4.8 EventBus 语义：会话级 vs 全局

**建议**：
- 明确文档说明：当前 `EventBus` 是**请求/会话级**实例，由 `StreamService` 每次请求创建；
- 在 API 层保留会话级总线，确保 SSE、审计、调试消费者天然隔离；
- 如果需要跨会话/全局监听（如插件生命周期、全局 metrics），使用全局 `CapabilityRegistry` 监听器或 OpenTelemetry，而不是全局 EventBus。

---

### 4.9 AuditLogger 与 EventBus 的“双路写入”落地

**关键事件清单**：
- `state.transition`
- `llm.request`
- `llm.response`
- `tool.result`
- `tool.error`
- `guard.triggered`

**双路策略**：
- **主路**：`loop.py` 直接调用 `AuditLogger` 写盘（当前已实现 `_maybe_write_audit_turn`）；
- **旁路**：`EventBus` 消费者（如 SSEAdapter）不依赖 AuditLogger；
- **失败降级**：如果 `EventBus` 队列满，按 `backpressure` 策略丢弃；AuditLogger 的写盘失败则记录到 stderr，不影响主循环；
- **去重原则**：同一事件不要在 EventBus 和 AuditLogger 两边各写一份全量内容；EventBus 发摘要，AuditLogger 写完整 payload。

---

## 5. 可优化项的落地方案

### 5.1 `GuardrailSystem.on_event` 接入 `EventBus`

已在 2.7 节给出实现。补充一点：`_on_guardrail_event` 应当过滤掉 `action="allow"` 的事件，只发布 `log` / `block`。

```python
async def _on_guardrail_event(result: GuardResult) -> None:
    if event_bus is None or result.action == "allow":
        return
    await _publish(
        "guard.triggered",
        {
            "layer": result.layer,
            "guard_name": result.guard_name,
            "action": result.action,
            "reason": result.reason,
            "metadata": result.metadata or {},
        },
    )
```

---

### 5.2 `hint.injected` 事件完整落地

除 2.7 节的 `loop_hints.py` 修改外，需要在 `loop.py` 中把 `event_bus`、`session_id`、`agent_name`、`turn_index` 透传进去：

```python
current_state = await check_and_inject_hints(
    tool_registry=tool_registry,
    artifact_store=artifact_store,
    consecutive_exploratory=consecutive_exploratory,
    current_state=current_state,
    agent_name=agent_name,
    event_bus=event_bus,
    session_id=session_id,
    turn_index=turn_index,
)
```

---

### 5.3 `AgentState` 状态变更统一走状态机 ✅ 已落地

已在 2.8 节给出渐进式迁移方案并落地。补充测试：

```python
def test_add_observation_does_not_set_status():
    state = AgentState.initial(task="test")
    state = state.model_copy(update={"status": "waiting_for_tool"})
    result = ExecutionResult(success=True, actor_type="tool", actor_name="echo")
    new_state = state.add_observation((result,), set_status=False)
    assert new_state.messages[-1].role == "tool"
    assert new_state.status == "waiting_for_tool"  # 不变
```

---

### 5.4 `SSEAdapter` 补齐未处理事件

已在 2.7 节给出。前端协议向后兼容：新增 SSE 类型不会导致旧前端报错，旧前端只忽略未知类型。

---

### 5.5 `ModelRouter.stream()` 实现 ✅ 已落地

已在 2.5 节给出并落地。补充集成测试：

```python
@pytest.mark.asyncio
async def test_router_streams_from_first_available_backend():
    backend1 = FakeBackend(name="b1", supports_streaming=True)
    backend2 = FakeBackend(name="b2", supports_streaming=True)
    router = ModelRouter([backend1, backend2])
    chunks = [c async for c in router.stream(ChatRequest(model="m", messages=()))]
    assert chunks == [TokenChunk(text="b1")]
```

---

### 5.6 清理测试警告

8 条警告来自 `tests/agent/test_cache_store.py` 和 `tests/agent/test_context_manager.py`，原因是非 async 函数被 `@pytest.mark.asyncio` 标记。

**修复方式**：

```python
# 旧
@pytest.mark.asyncio
 def test_no_refs_returns_unchanged(self, cache_store):
     ...

# 新
 def test_no_refs_returns_unchanged(self, cache_store):
     ...
```

可以用 grep 批量定位：

```bash
grep -n "@pytest.mark.asyncio" tests/agent/test_cache_store.py tests/agent/test_context_manager.py | grep -B1 "def test_"
```

---

## 6. 实施优先级与验收

### 6.1 优先级

| 优先级 | 事项 | 预计影响 | 验收方式 |
|---|---|---|---|
| P0 | 更新迁移计划文档（状态、不一致说明） | 文档对齐 | 人工评审 |
| P0 | `guard.triggered` / `hint.injected` 事件发射 + SSE 分发 | 事件完整 | 新增/更新单元测试 |
| P0 | `Settings` 配置映射落地 | 可运营 | 环境变量覆盖测试 |
| P1 | `AgentStateMachine` 事件/transition_id | 审计链完整 | ✅ 已更新 `test_state_machine.py` |
| P1 | `ModelBackend` 协议属性 + `ModelRouter.stream()` | 路由能力完整 | ✅ `test_backends.py` 通过 |
| P1 | 护栏默认模式对齐 | 降低误杀 | 集成测试 |
| P2 | 会话树持久化 + fork/rewind API | 高级功能可用 | ✅ DB schema + `test_fork_rewind.py` |
| P2 | 工具版本迁移 SOP | 运营规范 | ✅ 文档已落地 |
| P2 | Metrics / Alert / 前端适配 | 可观测 | ✅ 指标已接入 + 文档已落地 |
| P3 | 清理测试警告 | CI 输出干净 | `pytest -q` 无警告 |

### 6.2 推荐执行顺序

1. **第 1 轮（文档 + 低风险代码）**：
   - 更新 `pi-architecture-migration-plan.md`；
   - 补齐 `ModelBackend` 属性；
   - 实现 `guard.triggered` / `hint.injected`；
   - `SSEAdapter` 分发新事件；
   - `Settings` 配置映射。

2. **第 2 轮（核心重构）**：
   - `AgentStateMachine` 事件/transition_id；
   - `AgentState` 状态变更上移到状态机；
   - `ModelRouter.stream()` 实现。

3. **第 3 轮（运营化）**：
   - 会话树 DB schema + API；
   - Metrics / Alert；
   - 前端适配。

4. **第 4 轮（清理）**：
   - 测试警告；
   - 全量测试 + 灰度验证。

### 6.3 回滚策略

- 所有新增配置项都有默认值，关闭开关即可回滚到旧行为；
- 事件发射失败不得抛异常到主循环，避免阻断 agent 执行；
- DB migration 只追加 nullable 列，downgrade 可安全回退；
- 如果 `AgentStateMachine` 事件重构引入回归，可恢复 `strict=False` 并禁用 `event_bus` 参数。

---

## 7. 结论

本方案把上一阶段评审发现的所有问题划分为**文档修正、代码补齐、架构运营化**三类，每一类都给出了可直接执行的文件路径、代码片段与测试建议。建议按 P0→P3 顺序分批落地，每批完成后运行 `uv run pytest -m "not integration"` 与 `uv run ruff check`，确保迁移过程持续可交付。
