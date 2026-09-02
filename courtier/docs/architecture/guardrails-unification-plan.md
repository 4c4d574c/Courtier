# 权限门控收编 Guardrails 体系 — 实施方案

## 背景与目标

当前权限执行与护栏体系是两套并行机制：`PermissionGate` 硬编码在工具分派路径上（`loop_phases.py:316-344` 逐调用 `permissions.check()`），guardrails 则是半空脚手架——四层派发点全接通，但生产只注册两个 post_tool 行为护栏，另有五个休眠的玩具 guard。

目标：按 Pi 迁移计划的既定方向（`pi-architecture-migration-plan.md` §4.3、line 797），把权限执行收编进统一的 `GuardrailSystem`——新增**每调用粒度的 `tool_call` 层**（fail-closed 派发），`PermissionGate` 的两条规则移植为该层的两个 guard 后原类删除；同步删除五个休眠脚手架；打通 capability 元数据驱动的规则声明与域包注册 guard 的扩展机制。**不改动 HookChain**（讨论中已评估并否决用钩子链承载权限：缺每调用事件、否决词汇与 fail-closed 派发三样）。

## 对齐结论（已与用户确认，2026-09-02）

1. **动机**：与 Pi guardrails/capability 体系统一，成体系设计，不做长期迭代补丁。
2. **路线**：扩展 guardrails 体系（新增 `tool_call` 每调用层），而非 HookChain 方案或"门内规则可插拔"方案。
3. **双契约失效语义**：轮级行为层维持 fail-open（异常→跳过，现状）；`tool_call` 权限层 fail-closed（异常→拒绝）。层默认值编码各层的失效安全方向，写入 base.py 契约。
4. **影子模式**：`tool_call` 层 mode 默认 `block`；`log` 仅作为新规则灰度的手动操作。
5. **五个休眠脚手架 guard 全部删除**：`SensitiveInputGuard` / `EmptyOutputGuard` / `RefusalOutputGuard` / `DangerousToolGuard` / `RepeatedToolGuard`。`GuardResult.log` 动作与每层 log-mode 机制保留（影子灰度的承重机制）；四层派发点全部保留。
6. **Phase 3 全范围纳入本期**：capability 元数据驱动规则 + 域包注册 guard。
7. **`require_confirmation` 死代码**借机删除。

## 侦察结论（已验证事实）

- **PermissionGate**（`permissions/gate.py`）：两级规则——名级禁用（`_blocked` 集合，生产无调用方）+ 参数级路径策略（`read/edit/write` + `resolve()` 归一 + `allowed_roots`，`gate.py:70-91`，解析异常 fail-closed）。`require_confirmation`/`needs_confirmation` 无消费者。唯一生产配置：`agent_service.py:191` 构造门注入 `allowed_roots=[memory_home, session_workspace]`。
- **调用点与拒绝合成**：`loop_phases.py:316-344` 逐调用检查；拒绝合成（`ExecutionResult.from_error`、`error_code: permission_denied`、`ToolExecutionRecord`、`on_tool_result` SSE 回调）全部在主调函数内——这些留在循环侧，guard 只做决策。
- **GuardrailSystem 骨架**：`base.py` 定义 `Guardrail` Protocol（`check(context) -> GuardResult`）、`GuardResult`（allow/log/block）、`GuardLayer = input|output|tool|post_tool`；`guardrail_system.py` 每层模式 off/log/block/allow，output 默认 log；**`check()` 异常即跳过继续（fail-open，system.py:53-57）**。装饰性缺陷：`GuardResult.block` 硬编码 `layer="input"`（`base.py:44`，system 层会重包装，仅影响原始对象）。
- **派发点四层全接通**：input（`loop.py:375`，收到完整 `messages` 历史）、output（676）、tool（707，轮级 block→`blocked` 终态）、post_tool（808）。
- **生产注册仅两个**：`loop.py:1142-1146` 默认构造注册 `ExploreLoopGuard` + `BusinessArtifactProgressGuard`（均 post_tool）；tool_guard / input_guard / output_guard 三个文件的五个类零注册。
- **注入缝现成**：`agent_loop` 已有 `guardrail_system: GuardrailSystem | None` 参数（loop.py:960），None 时才默认构造——`build_agent` 接线无需改循环签名。
- **CapabilityRegistry 已存在**：`agent/core/capability.py`（`Capability`：type/name/provider/meta/instance；type 含 tool/skill/agent/resource/route/checker）。
- **permissions 调用链（删除范围）**：`agent_service.py:321,342` → `orch.py:44,127` 与 `runtime.py:83,523` → `base.py:196,233,563` → `agent_loop(permissions=...)`。全内部 API。
- **HookChain 现状（本方案不动）**：仅 `pre_think`/`post_observe` 两事件派发；拦截器返回 `AgentState` 无否决词汇；`continue_on_error=True` 默认。
- **观测面现成**：`guard.triggered` 事件（`loop.py:1120-1130`）+ `record_guardrail_blocked` 指标（system.py:87）。
- **Pi 迁移计划对照**：line 731 定义 tool 层为"权限门后、工具执行前检查"（权限门独立存在）；line 797 预定 GuardrailSystem 依赖 Capability schema。本次收编构成对 line 731 的偏差（见偏差记录）。

## 总体设计

### 1. 分层模型：新增 `tool_call` 层

`GuardLayer` 扩展 `"tool_call"`。语义契约三句话：**逐调用求值、单调用拒绝（被拒调用合成自身错误结果，运行继续）、永不阻断整轮**。轮级拦截仍归 tool 层，两者互不越界。

tool phase 内执行顺序（相对关系与现状逐位一致）：

```
output（轮）→ tool（轮）→ 逐调用：参数解析 → tool_call 层 → 执行
                          → observe → post_tool（轮）
```

### 2. 决策词汇表

- `Guardrail` 协议新增**可选方法** `check_call(call, context) -> CallGuardResult`（deny/allow + reason + metadata 含 `error_code`）；未实现该方法的 guard 不参与 `tool_call` 层——两种否决粒度的词汇不互相污染。
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

依赖关系：T0 独立可先行；T1→T2→T3 串行；T4、T5 依赖 T3；T7、T8 依赖 T3（机制就绪后可并行）；T6、T9 收尾。

## 明确不做（本期）

- HookChain 任何改动（不引入 `pre_tool_call` 事件）；hooks 与 guardrails 的机制层统一留给 Pi 迁移后续。
- 输入侧敏感信息检测的真实重实现（中文 PII 模式等）——待真实需求按域 guard 机制立项（T8 的机制即其落点）。
- `require_confirmation` 的用户交互链路设计。
- 轮级行为层 fail-open 语义的任何变更。
- 插件侧声明的权限规则（插件在进程外，违背 tool_call 层纯内存约束；如需经 host 侧适配另议）。
- 模型输出拒绝（refusal）的重试/换模型策略。

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

## 验收标准

- 现有权限测试全量迁移后 `uv run pytest` 全绿；真机越界路径拒绝文案与改前逐字一致。
- 故障注入单测：block mode 下 guard 抛异常 → 该调用被拒（非放行）；log mode 只记不拦。
- 五个脚手架类在代码库中不存在，grep 无残留引用。
- 会话根注入生效（子代理/无根路径 allow-all 与现状一致）。
- T7/T8：capability 声明 `path_policy` 的工具受路径策略管、未声明回退名单；声明 guard 的域包过 `validate-domain`、激活后生效、去激活注销。
- 每任务独立 conventional commit；实施偏差追加记录于本文件。

## 实施状态（滚动更新）

- 2026-09-02：需求对齐完成（对齐结论 7 条），方案成文，待批准后按 T0 起步。
