# 权限门控收编 Guardrails 体系 — 实施方案

## 背景与目标

当前权限执行与护栏体系是两套并行机制：`PermissionGate` 硬编码在工具分派路径上（`loop_phases.py:316-344` 逐调用 `permissions.check()`），guardrails 则是半空脚手架——四层派发点全接通，但生产只注册两个 post_tool 行为护栏，另有五个休眠的玩具 guard。

目标：按 Pi 迁移计划的既定方向（`pi-architecture-migration-plan.md` §4.3、line 797），把权限执行收编进统一的 `GuardrailSystem`——新增**每调用粒度的 `tool_call` 层**（fail-closed 派发），`PermissionGate` 的两条规则移植为该层的两个 guard 后原类删除；同步删除五个休眠脚手架；打通 capability 元数据驱动的规则声明与域包注册 guard 的扩展机制。权限不经钩子链承载（HookChain 缺每调用事件、否决词汇与 fail-closed 派发三样，讨论中否决）；同期完成 **hooks 与 guardrails 的机制统一**——HookChain 并入 GuardrailSystem 统一管线（§9），事件总线保持独立。

## 对齐结论（已与用户确认，2026-09-02）

1. **动机**：与 Pi guardrails/capability 体系统一，成体系设计，不做长期迭代补丁。
2. **路线**：扩展 guardrails 体系（新增 `tool_call` 每调用层），而非 HookChain 方案或"门内规则可插拔"方案。
3. **双契约失效语义**：轮级行为层维持 fail-open（异常→跳过，现状）；`tool_call` 权限层 fail-closed（异常→拒绝）。层默认值编码各层的失效安全方向，写入 base.py 契约。
4. **影子模式**：`tool_call` 层 mode 默认 `block`；`log` 仅作为新规则灰度的手动操作。
5. **五个休眠脚手架 guard 全部删除**：`SensitiveInputGuard` / `EmptyOutputGuard` / `RefusalOutputGuard` / `DangerousToolGuard` / `RepeatedToolGuard`。`GuardResult.log` 动作与每层 log-mode 机制保留（影子灰度的承重机制）；四层派发点全部保留。
6. **Phase 3 全范围纳入本期**：capability 元数据驱动规则 + 域包注册 guard。
7. **`require_confirmation` 死代码**借机删除。
8. **hooks 与 guardrails 统一**（2026-09-02 补充定案）：并入同一派发对象（GuardrailSystem v2 统一管线），三类处理器词汇分离——guards（裁决：`GuardResult` / `CallGuardResult`）、interceptors（适配：收/改 `AgentState`，恒 fail-open）、observers（记录：无返回、异常恒吞）；每 scope 固定顺序**适配→裁决→记录**；`tool_call` scope 禁止 interceptors（裁决独占）；HookChain 与 `agent/hooks/` 包删除，`pre_search` 死事件删除；EventBus 不并入。

## 侦察结论（已验证事实）

- **PermissionGate**（`permissions/gate.py`）：两级规则——名级禁用（`_blocked` 集合，生产无调用方）+ 参数级路径策略（`read/edit/write` + `resolve()` 归一 + `allowed_roots`，`gate.py:70-91`，解析异常 fail-closed）。`require_confirmation`/`needs_confirmation` 无消费者。唯一生产配置：`agent_service.py:191` 构造门注入 `allowed_roots=[memory_home, session_workspace]`。
- **调用点与拒绝合成**：`loop_phases.py:316-344` 逐调用检查；拒绝合成（`ExecutionResult.from_error`、`error_code: permission_denied`、`ToolExecutionRecord`、`on_tool_result` SSE 回调）全部在主调函数内——这些留在循环侧，guard 只做决策。
- **GuardrailSystem 骨架**：`base.py` 定义 `Guardrail` Protocol（`check(context) -> GuardResult`）、`GuardResult`（allow/log/block）、`GuardLayer = input|output|tool|post_tool`；`guardrail_system.py` 每层模式 off/log/block/allow，output 默认 log；**`check()` 异常即跳过继续（fail-open，system.py:53-57）**。装饰性缺陷：`GuardResult.block` 硬编码 `layer="input"`（`base.py:44`，system 层会重包装，仅影响原始对象）。
- **派发点四层全接通**：input（`loop.py:375`，收到完整 `messages` 历史）、output（676）、tool（707，轮级 block→`blocked` 终态）、post_tool（808）。
- **生产注册仅两个**：`loop.py:1142-1146` 默认构造注册 `ExploreLoopGuard` + `BusinessArtifactProgressGuard`（均 post_tool）；tool_guard / input_guard / output_guard 三个文件的五个类零注册。
- **注入缝现成**：`agent_loop` 已有 `guardrail_system: GuardrailSystem | None` 参数（loop.py:960），None 时才默认构造——`build_agent` 接线无需改循环签名。
- **CapabilityRegistry 已存在**：`agent/core/capability.py`（`Capability`：type/name/provider/meta/instance；type 含 tool/skill/agent/resource/route/checker）。
- **permissions 调用链（删除范围）**：`agent_service.py:321,342` → `orch.py:44,127` 与 `runtime.py:83,523` → `base.py:196,233,563` → `agent_loop(permissions=...)`。全内部 API。
- **HookChain 生产使用面（P4 并入）**：**零生产注册**（无任何 `hooks.register`/`register_observer` 调用方）；仅空默认构造（`base.py:232`，`build_agent` 不传 hooks）；派发点仅两处（`loop.py:361` pre_think、`loop.py:911` post_observe）+ `set_context`（`loop.py:1150-1151`）；`PRE_SEARCH` 导出但从未派发；import 面仅 `base.py:25` / `orch.py:16` / `loop.py:48`；runtime 不触及。与五个休眠 guard 同性质的脚手架。
- **观测面现成**：`guard.triggered` 事件（`loop.py:1120-1130`）+ `record_guardrail_blocked` 指标（system.py:87）。
- **Pi 迁移计划对照**：line 731 定义 tool 层为"权限门后、工具执行前检查"（权限门独立存在）；line 797 预定 GuardrailSystem 依赖 Capability schema；**全文未提及 HookChain 的映射与归宿**。本次收编构成对 line 731 的偏差（见偏差记录），hooks 归宿由本期定案（§9）。

## 总体设计

### 1. 分层模型：新增 `tool_call` 层

`GuardLayer` 扩展 `"tool_call"`。语义契约三句话：**逐调用求值、单调用拒绝（被拒调用合成自身错误结果，运行继续）、永不阻断整轮**。轮级拦截仍归 tool 层，两者互不越界。

tool phase 内执行顺序（相对关系与现状逐位一致）：

```
output（轮）→ tool（轮）→ 逐调用：参数解析 → tool_call 层 → 执行
                          → observe → post_tool（轮）
```

### 2. 决策词汇表

- `Guardrail` 协议新增**可选方法** `check_call(call, context) -> CallGuardResult`（action 三值 `allow / deny / confirm` + reason + metadata 含 `error_code`；`confirm` 本期仅词汇预留，挂起交互链路见 `confirmation-interaction-plan.md`）；未实现该方法的 guard 不参与 `tool_call` 层——两种否决粒度的词汇不互相污染。
- `GuardrailSystem.check_call(layer, call, context)`：收集该层全部 guard 决策，任一 deny 即 deny，结果携带 `guard_name`。
- **循环独占拒绝合成权**：`loop_phases.py` 现有拒绝分支原样保留（文案、`error_code: permission_denied`、records、SSE 回调），仅决策来源从 `permissions.check(tool_call)` 换成 `guardrail_system.check_call(...)`。

### 3. fail-closed 派发（承重墙）

- `tool_call` 层派发规则与 `check()` 相反：**block mode 下 guard 抛异常 → 按 deny 处理**（通用内部原因 + `error_code: permission_error`），不适用 log-and-continue。log mode 下本就无强制力，异常与普通结果同样只记录。
- 该层**不引入超时机制**，并写成注册约束：tool_call 层 guard 必须是纯内存判定（集合查找 / 路径 resolve），无 IO。
- base.py 模块文档写明双契约：轮级行为层 fail-open（守护运行成本，上游有步数/预算硬底线），权限层 fail-closed（检查器失效即拒绝，对手最优手段只能换来 denial）。
- 顺带修复 `GuardResult.block` 的 `layer="input"` 硬编码。

### 4. 模式语义

`GuardrailSystem` 新增 `tool_call_mode: GuardMode = "block"`。`log` = 影子模式（记 `guard.triggered` + 指标，不实际拦截）——新权限规则先影子观察再切 block 的灰度通道；`off`/`allow` 沿用现有短路语义。

### 5. 规则迁移映射

| PermissionGate 现有规则 | 去向 |
|------|------|
| 名级禁用（`_blocked`） | `ToolDisabledGuard`（tool_call 层，`check_call`） |
| 路径策略（roots + read/edit/write + resolve） | `PathPolicyGuard`（tool_call 层，roots 构造注入，文案与 gate 逐字一致） |
| `require_confirmation` 注册表 | 删除（无消费者） |
| `PermissionGate` 类、`agent_loop`/`Agent`/`OrchestratorAgent`/`AgentRuntime` 的 `permissions` 参数与属性 | 删除（侦察结论调用链全清单） |

### 6. 接线

- `build_agent`：构造会话级 `GuardrailSystem`（loop 护栏 + `ToolDisabledGuard` + `PathPolicyGuard([memory_home, session_workspace])`），经现有 `guardrail_system` 参数传入 `agent_loop`。
- 循环默认构造（参数为 None 时，`loop.py:1142`）：仅 loop 护栏、**无权限 guard**——与现状 `PermissionGate()` 的 allow-all 精确对齐。子代理（不经 build_agent 构造的路径）沿用默认构造，行为不变；对齐验证列入任务清单。

### 7. capability 元数据驱动（Phase 3b）

工具的 `Capability.meta["permission"]` 携带策略声明（如 `{"path_policy": true}`）；`PathPolicyGuard` 判定"哪些工具受路径策略管"时改为读注册表声明，**未声明工具回退现有 `DEFAULT_PATH_POLICY_TOOLS` 名单**（内置工具兼容）。实施时核实 ToolRegistry → CapabilityRegistry 的注册覆盖面，缺口工具显式补声明。

### 8. 域注册 guard（Phase 3a）

- 域包 `config/domain.yaml` 新增 `guards:` 声明（类路径清单）；`courtier validate-domain` 校验可导入、layer 声明合法、（tool_call 层时）纯内存约束的静态提示。
- 注册双路径：`build_agent` 按 `SessionRecord.active_domains` 在构造时注册（每请求重建 agent 的现有限定下无需跨轮变更）；`DomainActivator.activate()` 运行中激活时向存活 agent 的系统补注册。注销对称（activate 反向 / 会话结束随 agent 丢弃）。
- `GuardrailSystem.register` 支持 owner 归属（可选参数或返回可取消 handle），域 guard 以域名为 owner。
- 本期只落机制 + 测试用 dummy guard，不为 docaudit 新增真实域 guard。

### 9. hooks 并入：统一管线（GuardrailSystem v2）

**模型：一个派发对象、三类处理器词汇、一张 scope 表。**

- **三类词汇分离**（各自失效语义恒定，不设开关）：
  - **guards（裁决）**：轮级 `check(context) -> GuardResult` / 每调用 `check_call(call, ctx) -> CallGuardResult`；失效语义按 scope 类别——裁决性 scope（`tool_call`）fail-closed，行为性 scope（其余）fail-open（即 §3 双契约，此处上升为 scope 类别属性）。
  - **interceptors（适配）**：收/返回 `AgentState`；**恒 fail-open**（异常记日志跳过，不再有 `continue_on_error` 开关）；支持每注册超时 + 系统级 `interceptor_timeout`（默认 None = HookChain 现状）。
  - **observers（记录）**：无返回，异常恒吞。
- **每 scope 固定顺序：适配 → 裁决 → 记录**。适配在前保证 guard 裁决的是"适配后将要真实发生的状态"（适配无法绕过裁决）。生产现状 hooks 零注册，规则化无可观察行为变化；仅一处测试内先后断言需更新（原 POST_OBSERVE 拦截器在 post_tool guard 之后派发，并入后移到 guard 之前）。
- **scope 表**（guard 层与 hook 事件的合并关系）：

| scope | 合并来源 | interceptors | guards | observers |
|---|---|---|---|---|
| `pre_think` | 原 PRE_THINK 事件 + input 层 | ✓ | input 层 | ✓ |
| `output` | 原 output 层 | ✓ | output 层 | ✓ |
| `tool` | 原 tool 层（轮级） | ✓ | tool 层 | ✓ |
| `tool_call` | 本次新增（每调用） | **✗ 裁决独占**（禁止改状态/注入调用，注册即拒绝） | tool_call 层 | ✓（逐调用审计） |
| `post_tool` | 原 post_tool 层 + POST_OBSERVE 事件 | ✓ | post_tool 层 | ✓ |

- `PRE_SEARCH` 从未派发，随包删除。
- **API**：`GuardrailSystem` 保留类名与现有 guard 注册 API；新增 `register_interceptor(scope, handler, *, priority, timeout=None)` / `register_observer(scope, handler)` / `set_context(agent_name, session_id, **meta)`（原 HookContext 会话上下文并入 GuardContext 字段）；派发入口统一为 `run_scope(scope, ctx)`（轮级）与 `check_call`（每调用）。loop 内 `hooks.run` 两处调用点与 `guardrail_system.check` 四处调用点收敛为 scope 派发；`hooks.set_context`（loop.py:1150-1151）改为管线 `set_context`。
- **删除面**：`HookChain`/`HookHandle`/`HookContext` 与 `agent/hooks/` 包整体删除；`Agent.hooks`、`agent_loop` 的 `hooks` 参数删除——调用链与 `permissions` 同构（`base.py:25,232,563`、`orch.py:16` 及其 hooks 传参、`loop.py:48,946,361,911,1150-1151`；删前 grep 复核）。
- **EventBus 边界**：不并入。EventBus 是异步扇出（SSE/遥测），无返回契约，不承担裁决与适配；管线经既有 `on_event` 与 loop 的 `state.transition`/`guard.triggered` 发布镜像到总线。机制层统一到管线为止，总线保持独立。

## 任务拆分（每任务独立提交，conventional commits）

- **T0 脚手架清理（先于一切）**：删除五个休眠 guard 类（`tool_guard.py` 整文件、`input_guard.py` 整文件、`output_guard.py` 整文件）及测试/导出残留（`__init__.py`、pi 计划文档不动）。`uv run pytest` 全绿。
- **T1 base 词汇**：`CallGuardResult` + `Guardrail.check_call` 可选方法 + `GuardLayer` 扩展 `"tool_call"` + `GuardResult.block` layer 修复 + base.py 双契约文档。单测：协议可选性（无 check_call 的 guard 不参与该层）。
- **T2 system 派发**：`GuardrailSystem.check_call` + `tool_call_mode` 字段 + 指标复用。单测：任一 deny 即 deny、block mode 异常→deny（`error_code: permission_error`）、log mode 影子（记不拦）、off/allow 短路。
- **T3 门控移植**：`ToolDisabledGuard` + `PathPolicyGuard`（逻辑与文案从 gate.py 原样移植）+ `loop_phases` 决策来源切换、拒绝合成保留。单测：现有 gate 测试全量迁移（越界/穿越/symlink/异常路径/名级禁用）+ 拒绝路径回归（`error_code`、records、`on_tool_result`、同批其余调用不受影响）。
- **T4 会话接线**：`build_agent` 构造会话 GuardrailSystem（护栏 + 权限 guards + roots）传参；循环默认构造去权限化。单测：会话根注入生效、子代理默认 allow-all 对齐、参数优先于默认构造。
- **T5 删除 PermissionGate**：按调用链清单删 `gate.py`、各构造/传参点、`permissions` 参数与属性、`require_confirmation`（删前 grep 确认无新增读者）。全量回归。
- **T6 文档同步**：根 `AGENTS.md` §5.1/§7.1/§10（权限描述改 guardrails 口径）、`courtier/CLAUDE.md`、本文件实施状态；Pi 迁移计划偏差记录（见下节）。
- **T7 capability 声明驱动**：`meta["permission"]` 声明消费 + 注册覆盖面核实与补声明 + 回退名单。单测：声明生效、未声明回退、覆盖面清单测试。
- **T8 域注册机制**：domain.yaml `guards:` 解析 + validate-domain 校验 + 双路径注册/注销 + owner 归属 + dummy guard 测试。单测 + `validate-domain` 对 docaudit 的回归。
- **T9 收尾**：`uv run pytest` 全量 + webui `npm test`/`npm run build`（webui 预期零改动，构建验证）+ 真机冒烟（上传文档会话：一次正常读答 + 一次越界路径拒绝，比对 `permission_denied` 文案与改前一致）+ 实施状态追加。
- **T10 hooks 并入（P4）**：scope 表落地（pre_think = input 层 + PRE_THINK；post_tool = post_tool 层 + POST_OBSERVE；删 `pre_search`）+ `GuardrailSystem` interceptors/observers 注册、`set_context`、`run_scope` 收敛（`hooks.run`×2、`check`×4 调用点合并）+ `HookChain`/`agent/hooks/` 包删除 + `Agent.hooks`/`agent_loop` hooks 参数链删除。单测：scope 内顺序（适配→裁决→记录）、interceptor fail-open 与超时、observer 吞异常、tool_call scope 拒绝 interceptor 注册、旧 hooks 测试全量迁移。
- **T11 文档同步（hooks 部分）**：根 `AGENTS.md` §5.1（`agent/hooks/` 目录条目删除）、§5.3（执行顺序口径改统一管线）、`courtier/CLAUDE.md` 对应条目。

依赖关系：T0 独立可先行；T1→T2→T3 串行；T4、T5 依赖 T3；T7、T8 依赖 T3（机制就绪后可并行）；T10 依赖 T2（scope 派发机制就绪即可，与 T7/T8 可并行）；T6、T11、T9 收尾。

## 明确不做（本期）

- EventBus 并入统一管线（异步扇出与裁决/适配词汇分离是机制边界，不是待办；见 §9 末条）。
- 输入侧敏感信息检测的真实重实现（中文 PII 模式等）——待真实需求按域 guard 机制立项（T8 的机制即其落点）。
- `require_confirmation` 的交互链路**实现**——方案独立成篇（`confirmation-interaction-plan.md`，依赖本计划 T1-T3 先行）；本期 T1 仅预留 `confirm` 词汇，不实现挂起。
- 轮级行为层 fail-open 语义的任何变更。
- 插件侧声明的权限规则（插件在进程外，违背 tool_call 层纯内存约束；如需经 host 侧适配另议）。
- 模型输出拒绝（refusal）的重试策略——独立成篇（`refusal-retry-plan.md`，无跨计划依赖）；T0 删除的 `RefusalOutputGuard` 由其检测器替代，无代码继承。

## 回归红线

- 拒绝错误文案逐字节一致（含允许根清单格式）；`error_code: permission_denied` 与 metadata 结构不变。
- 单调用拒绝语义不变：被拒调用不影响同批其余调用；运行绝不因权限拒绝熔断。
- 拒绝合成路径行为不变：`ToolExecutionRecord`、`on_tool_result` SSE、审计记录。
- 会话安全行为不变：会话根（memory_home + session_workspace）继续生效；无根场景 allow-all 不变。
- 四个既有层语义零变更：fail-open 保持、`ExploreLoopGuard`/`BusinessArtifactProgressGuard` 行为不变、output 层 log 姿态不变。
- `guard.triggered` 事件与 `record_guardrail_blocked` 指标向后兼容（只新增 guard_name 值域，不改结构）。

## 偏差记录（相对 pi-architecture-migration-plan）

- **line 731**「tool：权限门后、工具执行前检查」：权限门不再作为独立组件存在，收编为 `tool_call` 层 guard（每调用、fail-closed）；tool 层维持轮级描述不变。
- **line 797**「GuardrailSystem 依赖 Phase 3 Capability schema 做工具参数校验」：本期先行落权限声明消费（`meta["permission"]`）；工具参数校验仍留待 capability 后续。
- 原 plan 无 `ToolDisabledGuard` / `PathPolicyGuard` / `CallGuardResult` 命名，命名记录于本文件。
- **hooks 归宿**：Pi 迁移计划全文未提及 HookChain 的映射与归宿；本期定案 HookChain 并入 GuardrailSystem 统一管线（三类词汇、scope 表，§9），`pre_search` 死事件删除，EventBus 保持独立。

## 验收标准

- 现有权限测试全量迁移后 `uv run pytest` 全绿；真机越界路径拒绝文案与改前逐字一致。
- 故障注入单测：block mode 下 guard 抛异常 → 该调用被拒（非放行）；log mode 只记不拦。
- 五个脚手架类在代码库中不存在，grep 无残留引用。
- 会话根注入生效（子代理/无根路径 allow-all 与现状一致）。
- T7/T8：capability 声明 `path_policy` 的工具受路径策略管、未声明回退名单；声明 guard 的域包过 `validate-domain`、激活后生效、去激活注销。
- T10：grep 无 `HookChain`/`agent/hooks` 残留；scope 内顺序（适配→裁决→记录）单测绿；interceptor 异常跳过不阻断、tool_call scope 拒绝 interceptor 注册；旧 hooks 测试全量迁移后全绿。
- `uv run pytest` 全量全绿；文档更新完成；每任务独立 conventional commit；实施偏差追加记录于本文件。

## 实施状态（滚动更新）

- 2026-09-02：需求对齐完成（对齐结论 8 条，含 hooks 统一定案），方案成文，待批准后按 T0 起步。
- 2026-09-02（批准后当日）：**T0-T11 全部落地**（d1305ef → 704a0e7 + 文档同步）。要点与偏差：
  - T2 派发实现与设计一致：首拒即胜短路、block mode 异常→deny（`errors.guard_call_failed`）、log 影子放行、confirm 透传（词汇预留）。
  - T4/T5 合并落地：会话系统只含无状态权限 guard（build_agent 构造，orchestrator/子代理共享）；**有状态 loop 护栏改为 agent_loop 运行期临时注册/finally 注销**（共享系统不能混用历史——设计文档 §6 未预见的实现细节）；PermissionGate 全链删除（gate.py + permissions/ 包 + agent_loop/Agent/orch/runtime 参数链）。
  - T10 hooks 并入：scope 表落地，`run_scope` 统一派发（think 阶段 pre_think scope 取代原 hook+input guard 两处调用；post_tool scope 单点合并原 post_tool guard 与 post_observe hook）；`PRE_SEARCH` 随包删除；EventBus 未动。
  - T7：PathPolicyGuard 读 `Capability.meta["permission"]["path_policy"]`（显式 opt-in/opt-out），未声明回退默认名单；build_agent 与 runtime 共享一个 CapabilityRegistry。
  - T8：domain.yaml `guards:` 声明 + `validate_domain` 校验（可导入/合法 layer）+ activator `_register_domain_guards` seam（owner=domain:<name>，replay 经 activate() 自动重注册）+ `unregister_owner` 对称注销；docaudit 未加真实 guard（按计划）。
  - 验收：tests/agent 全绿（1480）；全量 pytest 仅存与改动无关的 6 个实机环境失败（ES/OCR/MinIO，改动前基线相同）；webui `npm test` + `npm run build` 通过。
  - 顺手修复既有潜伏缺陷：`check()` 聚合此前只在"更严格"时替换结果，allow 携带的 metadata 被 initial 空白 allow 吞掉——post_tool 层 ExploreLoopGuard 的 `consecutive_exploratory` 计数因此从未流回循环，`check_and_inject_hints` 的两级升级（连续探索 ≥2 注入阻断提示、≥4 强制收尾，阈值早于护栏的 8）自 GuardrailSystem 迁移以来一直处于死分支。修复为 metadata 按执行顺序合并进聚合（严格度仍只决定动作/署名），镜像计数复活，升级链恢复设计行为。
  - **T9 真机冒烟（2026-09-02 当日补做，通过）**：真实 LLM 会话验证——① 越界 read 被拒，拒绝文案与旧门控逐字一致（含真实会话根清单：memory_home + session workspace）；② `tool_confirmation=[{tool:"write"}]` 热生效 → 写文件调用挂起并发出 `confirmation_requested` → API `approve_session` 裁决 → run 恢复完成、文件落盘、`approvedTools=["write"]` 持久化、`confirmation_resolved` 事件可见；③ 同会话续轮再写文件 **0 次确认**（approved 集合跨 agent 重建短路生效）；④ 冒烟发现并修复 `OrchestratorAgent.run` 未透传 `confirmation_handler` 的 kwarg 崩溃（runner 无条件传参、单测未覆盖 runner 路径——真机冒烟的价值所在）。
