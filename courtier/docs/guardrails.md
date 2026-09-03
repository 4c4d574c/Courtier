# Guardrails 统一管线 — 参考文档与扩展指南

> 对应实现：`courtier/agent/core/guardrails/`。设计与实施记录见
> `docs/architecture/guardrails-unification-plan.md`。本文是**使用与扩展**的参考手册。

GuardrailSystem（v2）是 agent 运行的统一扩展/执法管线：一个派发对象、三类处理器
词汇、五个 scope。历史上独立的 `PermissionGate` 与 `HookChain` 均已收编进它（2026-09-02）。

## 1. 核心模型

### 1.1 Scope 表（派发点 × 守卫层）

| scope | 派发位置 | interceptors | guards（守卫层） | observers |
|---|---|---|---|---|
| `pre_think` | think 阶段开头，模型调用前 | ✓ | input | ✓ |
| `output` | 模型响应后、工具执行前 | ✓ | output | ✓ |
| `tool` | 工具批执行前（**轮级**） | ✓ | tool | ✓ |
| `tool_call` | **每个**工具调用分派前 | ✗（裁决独占） | tool_call | ✓ |
| `post_tool` | 工具执行/观察后 | ✓ | post_tool | ✓ |

每个 scope 内固定顺序：**适配（interceptors）→ 裁决（guards）→ 记录（observers）**。
适配在前保证裁决对象是"适配后将要真实发生的状态"。`pre_think`/`post_tool` 两个
scope 同时是前 HookChain 事件（PRE_THINK/POST_OBSERVE）的合并落点；
`tool_call` 禁止 interceptor 注册（会改状态/注入调用），observer 允许（逐调用审计）。

### 1.2 三类处理器词汇（失效语义恒定，不可配置）

| 词汇 | 签名 | 失效语义 |
|---|---|---|
| **guards（裁决）** | 轮级 `check(ctx) -> GuardResult`；每调用 `check_call(call, ctx) -> CallGuardResult` | 行为层（input/output/tool/post_tool）**fail-open**：异常→跳过；`tool_call` 层 **fail-closed**：异常→按拒绝处理（`error_code: permission_error`） |
| **interceptors（适配）** | `handler(ctx) -> AgentState \| None`（None = 不变） | **恒 fail-open**：异常记日志跳过；支持每注册超时 + 系统级 `interceptor_timeout`（默认 None） |
| **observers（记录）** | `handler(ctx) -> None` | 异常恒吞 |

双契约的依据：行为层保护的是运行成本（上游有步数/预算硬底线，跳过的代价有界）；
`tool_call` 层是安全边界（fail-open 的权限系统只是日志系统）。

### 1.3 决策词汇表

```python
# 轮级（base.py）
GuardResult.allow(guard_name, reason="", metadata=None)
GuardResult.log(guard_name, reason)          # 仅记录
GuardResult.block(guard_name, reason)        # 阻断本轮（转 blocked/completed 终态）

# 每调用（base.py）
CallGuardResult.allow(guard_name)
CallGuardResult.deny(guard_name, reason, error_code="permission_denied")
CallGuardResult.confirm(guard_name, message)  # 确认链路预留（见 §3.3）
```

**聚合语义**：动作/署名取"最严格"（allow < log < block，先到先得），**metadata 按
执行顺序合并自全部已执行结果**（后者覆盖同名键）——guard 想向循环传状态
（如 `ExploreLoopGuard` 的 `consecutive_exploratory` 计数）就放 allow 的 metadata。

### 1.4 每层模式（`GuardMode`）

`off`（跳过整层）/ `log`（**影子模式**：只记 `guard.triggered` 事件不执法——新规则
灰度的手动通道）/ `block`（执行裁决，默认）/ `allow`（短路放行）。
`tool_call_mode` 默认 block；log 模式下守卫异常也不执法。

## 2. 内置守卫与生产接线

`build_agent`（`agent_service.py`）为每个会话构造**无状态**系统，orchestrator 与
所有子代理共享：

| Guard | 层 | 作用 | 关键参数 |
|---|---|---|---|
| `ToolDisabledGuard` | tool_call | 名级禁用（生产默认空名单） | `blocked` |
| `PathPolicyGuard` | tool_call | 文件路径白名单：`resolve()` 归一后必须落在根内；解析异常 fail-closed | `allowed_roots`（会话根：`memory_home` + 会话工作区）、`capability_registry` |
| `ConfirmationGuard` | tool_call | 名单内工具返回 confirm → 挂起等用户裁决（见 §3.3） | `rules`（settings `tool_confirmation`）、`approved_tools`（会话持久集合） |

**有状态**的行为护栏 `ExploreLoopGuard` / `BusinessArtifactProgressGuard`
（post_tool 层）由 `agent_loop` 在**每轮运行期临时注册、finally 注销**——共享会话
系统绝不能混用它们的历史。新增有状态 guard 必须走同样的生命周期。

循环在 `loop_phases.execute_tools_phase` 逐调用派发 `check_call`；拒绝合成
（错误结果、`error_code`、SSE、审计）由循环独占，guard 只决策。

## 3. 扩展方式

### 3.1 写一个轮级守卫（behavioral layer）

```python
from courtier.agent.core.guardrails import GuardContext, GuardResult

class MyOutputGuard:
    name = "my_output"            # 必需，聚合署名/事件用
    layer = "output"              # 必需，必须是 SCOPES 的守卫层之一

    async def check(self, context: GuardContext) -> GuardResult:
        ...
        return GuardResult.allow(self.name, metadata={"any": "state"})
```

- 想传状态给循环：放 `allow` 的 metadata（会合并进聚合结果）。
- 想"只告警不拦截"：返回 `log`，或把所在层 mode 设为 `log`（shadow）。
- 失败安全：异常会被吞掉跳过（fail-open）——不要在 check 里做不可靠的 IO。

### 3.2 写一个每调用守卫（tool_call 层）

```python
from courtier.agent.core.guardrails import CallGuardResult

class MyCallGuard:
    name = "my_call"
    layer = "tool_call"

    async def check_call(self, call: ToolCall, context: GuardContext) -> CallGuardResult:
        if something_bad(call):
            return CallGuardResult.deny(self.name, "拒绝原因（模型可见）")
        return CallGuardResult.allow(self.name)
```

**硬约束**：tool_call 层守卫必须是**纯内存判定**（集合查找、路径 resolve），
无 IO、无超时——该层 fail-closed，不可靠的检查等于拒绝用户的调用。
未实现 `check_call` 的 guard 不参与此层（协议 `CallGuardrail`）。

拒绝文案是**模型可见**的（会成为该调用的错误结果）：走 `errors.*` 模板
（`render_error`）或与既有 guard 一样的明确中文文案；改既有文案属于回归红线。

### 3.3 注册位置与确认链路

生产守卫在 `build_agent` 注册（`agent_service.py` 的 session 系统构造处）；
往那里加 guard 即对 orchestrator + 全部子代理生效。返回 `confirm` 的守卫
（`ConfirmationGuard` 模式）需要循环侧 handler 支持——目前只有 RunManager
桥接的确认链路（`docs/architecture/confirmation-interaction-plan.md`）；
子代理没有 handler，confirm 一律 fail-closed 拒绝（`confirmation_denied`）。

### 3.4 拦截器与观察者（运行期扩展）

```python
async def my_interceptor(context: GuardContext):
    # context.state 是当前 AgentState；返回新状态或 None
    return context.state

async def my_observer(context: GuardContext):
    ...  # 记录/审计；异常自动吞

system.register_interceptor("pre_think", my_interceptor, priority=10, timeout=2.0)
system.register_observer("post_tool", my_observer)
```

- `register_interceptor("tool_call", ...)` 直接拒绝（裁决独占 scope）。
- 未知的 scope 名拒绝；`set_context(agent_name=, session_id=, **meta)` 注入的
  会话上下文会合并进每次派发的 GuardContext。

### 3.5 域包声明 guard（推荐的产品化扩展路径）

域包在 `config/domain.yaml` 声明类路径，**零 core 改动**：

```yaml
# domains/<name>/config/domain.yaml
guards:
  - docaudit.guards.FormatGuard   # 可导入的 "<module>.<Class>"，无参构造
```

- **约束**：类必须有 `name` + 合法 `layer`；tool_call 层守卫须纯内存（见 §3.2）。
- `courtier validate-domain domains/<name>/` 会校验可导入性与 layer 合法性。
- 域激活时（含会话重建的重放路径）经 `DomainActivator` 注册进会话系统，
  owner = `domain:<name>`；`system.unregister_owner("domain:<name>")` 对称注销。
- 声明加载失败只告警跳过，不阻断域激活。
- 加载器：`guardrails/domain_guards.py`（`load_domain_guard` /
  `register_domain_guards`）；测试 dummy 见 `courtier/agent/testing/domain_guard.py`。

### 3.6 capability 元数据驱动策略

工具可通过 `Capability.meta` 声明策略，替代硬编码名单：

```python
CapabilityRegistry().register(Capability(
    type="tool", name="archive_dump",
    meta={"permission": {"path_policy": True}},   # 让自定义工具受路径白名单管
))
```

`PathPolicyGuard` 判定顺序：显式声明（含 `False` 显式豁免）→ 未声明回退
`DEFAULT_PATH_POLICY_TOOLS`（read/edit/write）。

## 4. 运维面

- **设置**（管理后台 → 运行守卫与预算，均 hot）：`tool_confirmation`
  （JSON `[{tool, message}]`）、`refusal_detection_enabled` / `refusal_patterns` /
  `refusal_retry_max`（模型行为恢复策略，**在管线外**——检测器在 think 阶段，
  不走 guard 派发）、`loop_*` 系列阈值。
- **事件/指标**：`guard.triggered` 总线事件（结构兼容，deny 映射 block）；
  `guardrail_blocked_total{layer, guard_name}`；`refusal_total{outcome}`。
- **确认 API**：`POST /api/sessions/{id}/confirmations/{cid}`
  （approve / approve_session / deny，owner 幂等）；会话详情带
  `pendingConfirmations`；跨标签页侧栏"待确认"徽标走 NotificationHub。

## 5. 修改守卫的检查清单

1. 新守卫放 `guardrails/` 下独立模块，导出进包 `__init__.py`。
2. 确定层与词汇：轮策略用 `check`，调用策略用 `check_call`（纯内存！）；观察用
   observer；改状态用 interceptor（非 tool_call）。
3. 决定注册点：会话级 → `build_agent`；域级 → domain.yaml；运行期 → 注册 API。
4. **有状态 guard 必须在 agent_loop 里走"运行期注册/finally 注销"**
   （参考 `loop.py` 的 `_run_guards`），否则共享系统会混用历史。
5. 新增/修改**模型可见文案**走 `errors.*` 模板（zh/en 双语 + `engine.py`
  RESERVED 注册）；既有拒绝文案逐字节不动（回归红线）。
6. 测试：`tests/agent/core/test_guardrails.py`（聚合语义）、
   `test_guardrail_call_dispatch.py`（派发/fail-closed）、
   `test_permission_guards.py`（路径策略，文案逐字节断言）、
   `test_domain_guards.py`（域机制）、`test_confirmation.py`（确认链路）。
7. 改完跑 `uv run pytest tests/agent -q`；涉及循环行为时补
   `tests/agent/test_loop.py` 级别的用例。

## 6. 已知坑（历史教训）

- **`OrchestratorAgent.run` 覆写**是参数透传的惯发故障点：给 `Agent.run` 加新
  kwarg（confirmation_handler、media_parts 各踩过一次）必须同步覆写签名并转发，
  否则所有 API 运行在启动调用处 TypeError（单测覆盖不到 runner 路径）。
- 聚合 metadata 按"执行顺序合并、同名后者覆盖"——依赖某个键存在时确认执行顺序；
  allow 之间的动作/署名保持系统占位（`guardrail_system`）。
- 有状态守卫注册进共享会话系统 = 跨代理历史污染（必须走运行期注册/注销）。
