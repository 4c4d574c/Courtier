# 守卫声明化注册方案（Guardrails Declarative Registration）

> 2026-09-04 · 状态：待批准
> 前置讨论：本轮对话完成需求对齐，全部决策点已由用户拍板（见 §1）。

## 0. 背景与目标

**现状（根因）**：守卫的注册点 = 构造点，"有哪些守卫"写死在两个组合根里：

- 会话级（无状态）：`agent_service.py:505-518` 硬编码 `ToolDisabledGuard()`、`PathPolicyGuard(allowed_roots, capability_registry)`、条件注册 `ConfirmationGuard(rules, approved_tools)`；
- run 级（有状态）：`loop.py:1134` 硬编码 `_run_guards = [ExploreLoopGuard(), BusinessArtifactProgressGuard()]`，每 `agent_loop` 新实例、finally 注销（`loop.py:1303`）。

新增任何守卫都必须改这两个核心文件。`docs/guardrails.md` §4.1 还把"无状态改 agent_service.py / 有状态改 loop.py"写成了扩展路径。

**已有资产**：域包已有声明通道——`domain.yaml` 的 `guards: ["<module>.<Class>"]`，`guardrails/domain_guards.py` 加载器（无参实例化、layer 合法性校验、owner 标签）、`domain/loader.py:114` 预校验、`runtime/activation.py` 每 run 回放。

**目标**：新增守卫 = 写一个类 + 在管理后台加一条声明，核心组合根对"新增"封闭（一次性改造后不再改动）。

**非目标**：

- 独立进程 JSON-RPC 插件贡献进程内守卫——守卫跑在代理热路径上，必须随宿主 venv 分发，这是架构边界，不在本方案内解决；
- entry points 自动发现（第三方 pip 包分发场景当前不存在，声明机制留有兼容空间）。

## 1. 决策记录（2026-09-04，用户已拍板）

| # | 决策点 | 结论 |
|---|--------|------|
| 1 | 有状态守卫是否开放声明 | **开放**，通过 `scope: run` 描述符，每 `agent_loop` 从配方实例化 |
| 2 | 内置守卫迁移范围 | **五个全部迁**（三个安全基线 + 两个行为守卫） |
| 3 | 管理入口 | **本次就做 admin 表单 UI**（不是裸 JSON） |
| 4 | 声明格式 | **对象列表**（name / class_path / scope / enabled 显式） |
| 5a | 构造参数注入 | **会话上下文工厂协议** `build(session_ctx)`（需要会话依赖的守卫实现；自足守卫走无参构造，域通道现状不破） |
| 5b | 基线锁定 | **可停用**。语义变更：admin 有权停用基线守卫（含 path_policy / confirmation），`settings_changes` 审计兜底；UI 提示明示后果。层模式（`tool_call_mode` 仅 block/log、守卫异常 fail-closed）**不变**——变的是单个守卫级开关 |

记录在案的现状勘误（随本方案修正文档）：`guardrails.md` §4.1 引用"§8 坑 3"实为坑 2；`unregister_owner` 目前无任何调用点（域没有运行期停用路径，靠每 run 重建系统自然回放）。

## 2. 设计

### 2.1 声明对象与 settings 键

新增 settings 键 **`guardrail_guards`**（guards 类「运行守卫与预算」，JSON 列表，热生效 = 下次 `build_agent`，与 layer mode 同语义）：

```json
[
  {"name": "tool_disabled",    "class_path": "courtier.agent.core.guardrails.permission_guards.ToolDisabledGuard",              "scope": "session", "enabled": true,  "builtin": true},
  {"name": "path_policy",      "class_path": "courtier.agent.core.guardrails.permission_guards.PathPolicyGuard",                "scope": "session", "enabled": true,  "builtin": true},
  {"name": "confirmation",     "class_path": "courtier.agent.core.guardrails.confirmation.ConfirmationGuard",                   "scope": "session", "enabled": true,  "builtin": true},
  {"name": "explore_loop",     "class_path": "courtier.agent.core.guardrails.loop_guardrails.ExploreLoopGuard",                 "scope": "run",     "enabled": true,  "builtin": true},
  {"name": "business_artifact","class_path": "courtier.agent.core.guardrails.loop_guardrails.BusinessArtifactProgressGuard",    "scope": "run",     "enabled": true,  "builtin": true}
]
```

- **列表顺序 = 派生顺序**（各 scope 内按列表相对顺序注册）。
- `builtin` 由系统管理：客户端提交时以服务端权威值覆盖（防止借壳：builtin 条目的 `class_path`/`scope`/`name` 不可改，`enabled` 可 false——这是 5b 的落点）。
- `name` 条目级唯一，作为管理标识；运行时聚合署名/事件仍用类自己的 `name` 属性（现状行为不变）。
- **种子**：设置未写入时，启动一次性迁移播种五条内置（仿模型池空池播种先例），管理后台读到真实条目；提供「恢复默认」按钮重写种子值。

### 2.2 加载器统一（新模块 `guardrails/registry.py`）

把 `domain_guards.py` 的加载逻辑泛化：

- `GuardDescriptor` dataclass：`name / class_path / scope: Literal["session","run"] / enabled / builtin`。
- `GuardSessionContext` dataclass：`session_workspace: Path`、`capability_registry`、`approved_tools: set[str]`、`settings`。`build_agent` 构造一次，传给加载器。
- **工厂协议**：守卫类实现 `@classmethod build(cls, ctx: GuardSessionContext) -> Guardrail` 即为"需要会话依赖"；否则无参构造（域通道现状语义）。
- 实例化时校验（沿 `load_domain_guard` 现状）：`name`/`layer` 属性存在、`layer ∈ SCOPES`；新增：`layer == "tool_call"` 且 `scope == "run"` → 非法（tool_call 层守卫必须 session 级、纯内存判定）。
- `domain_guards.py` 收敛为对 registry 的薄包装，`load_domain_guard` 对外签名不变（`domain/loader.py` 预校验继续可用）。

### 2.3 两个组合根的一次性改造

- **`agent_service.py`**：删三个硬编码 `register`；改为读 `guardrail_guards` → 过滤 `enabled && scope=="session"` → 按列表顺序注册（内置 owner=`builtin`，用户条目 owner=`declared`）。`GuardSessionContext` 就地构造。`declare_path_policy_tools`（capability 侧声明扫描）不动。
- **`loop.py`**：`GuardrailSystem` 增加字段 `run_descriptors: list[GuardDescriptor] = []`（默认空，所有现存构造点向后兼容）；`build_agent` 把 run 级已启用描述符附加到系统上；`loop.py:1134` 的硬编码列表换成遍历 `run_descriptors` 逐条实例化，finally 注销（`loop.py:1303`）逻辑不变。**有状态语义逐字节等价**：配方 → 每 `agent_loop`（编排器轮、每个子代理轮）新实例 → 用完丢弃，§8 坑 2 的跨代理历史污染防线原样保留。
- `ExploreLoopGuard` 被 loop.py 消费的 metadata（`consecutive_exploratory`，`loop.py:827`）路径不动；被停用时 `.get` 缺省路径已安全（T4 加测试确认）。
- `ConfirmationGuard` 行为等价性：现状是 `tool_confirmation` 非空才注册；迁移后常驻注册 + 空规则自然 inert（`check_call` 无规则命中返回 allow），T5 用测试确认两者等价。

### 2.4 域通道统一（收尾任务，可独立裁剪）

`domain.yaml` 的 `guards:` 继续接受字符串（= session 级无参，现状语义），**新增接受对象形式**（含 `scope: run`）。run 级描述符落 `system.run_descriptors`（owner=`domain:<name>`），随每 run 重建自然回放。域通道暂不开放 `build(ctx)` 工厂（域守卫保持无参构造；需要会话上下文的域场景目前不存在，记录为边界）。此项关闭"域包声明有状态守卫会踩坑 2"的现存隐患。

### 2.5 settings 保存校验

扩展点：`config.py` 的 pydantic `field_validator`（`tool_confirmation`、`llm_endpoint_keys` 同款模式）。校验规则：

- JSON 结构合法、每条目四字段类型正确；
- `class_path` 可导入、实例化形状正确（有 `build` 或无参构造）、`layer` 合法；
- `tool_call ∧ scope=run` 拒绝；
- `name` 唯一；builtin 条目不可改 `class_path`/`scope`/`name`。

校验失败挡保存、返回字段级错误。运行时兜底：加载失败记日志跳过（沿域守卫先例），不打挂 build。

### 2.6 管理后台 UI

- `SystemSettings.vue` 守卫组新增条目列表编辑器（`settingsForm.ts` / `settingsLabels.ts` 配套）：每行 name、class_path、scope 下拉（session/run）、enabled 开关、builtin 徽标；增删、上移下移、「恢复默认」。
- builtin 行：`class_path`/`scope`/`name` 只读，仅 enabled 可操作；停用基线守卫的行显示后果提示文案（如"停用后文件路径白名单不再管辖"）。
- 样式进 `admin.css`（组件内无 style 块的既有硬约束）。

## 3. 任务拆解（每任务一提交，Conventional Commits）

| # | 任务 | 主要触点 |
|---|------|----------|
| T1 | registry 统一加载器（Descriptor、GuardSessionContext、工厂协议、合法性校验）+ 单测 | `guardrails/registry.py`（新）、`domain_guards.py` 收敛 |
| T2 | `guardrail_guards` settings 键 + 启动播种 + 保存校验 + 单测 | `config.py`、启动迁移、admin 校验错误 |
| T3 | 五内置增加 `build()` 工厂 / 行为等价确认（ConfirmationGuard 空规则 inert、ToolDisabledGuard 空名单现状记录） | `permission_guards.py`、`confirmation.py`、`loop_guardrails.py` |
| T4 | agent_service 会话根改造（删硬编码、遍历声明、ctx 构造、owner 标签）+ 等价测试（默认全开=现状；停用=不注册） | `agent_service.py` |
| T5 | loop.py run 级改造（`run_descriptors` 字段、遍历实例化、finally 注销、metadata 缺省测试） | `guardrail_system.py`、`loop.py` |
| T6 | 前端守卫编辑器 + 前端测试 | `SystemSettings.vue`、`settingsForm.ts`、`settingsLabels.ts`、`admin.css` |
| T7 | 域通道统一（对象形式、run 描述符落点）+ 域测试 | `domain/loader.py`、`activation.py`、`registry.py` |
| T8 | 文档同步：guardrails.md（§4.1/§4.2/§4.4 注册故事重写、§5 接线地图、§8 坑 2 补域通道、坑编号勘误）、根 AGENTS.md §5.3、`docs/operations/settings-and-seeds.md`（若涉新键） | docs |
| T9 | 真机冒烟（按用户流程）：后台新增自定义 session 级 + run 级声明各一条验证生效；停用 path_policy 验证 5b 可停用 + 审计落 `settings_changes`；「恢复默认」；真实会话日志核对 `guard.triggered` 事件与指标标签 | 运行环境 |

依赖关系：T1 → T3 → (T4, T5) → T6/T7 → T8/T9。T4、T5 之间无依赖可并行推进。

## 4. 测试与验收

- `uv run pytest -m "not integration"` 全绿；`cd courtier/webui && npm test` 全绿。
- **行为等价清单**（默认五内置全开时与现状逐字节对齐）：tool_call 派生顺序（ToolDisabled → PathPolicy → Confirmation）、拒绝文案不动（回归红线，`tests/agent/test_permission_guards.py` 逐字节断言）、ConfirmationGuard 空规则 inert、`guard.triggered` 事件、`guardrail_blocked_total` 指标标签、ExploreLoopGuard metadata 聚合与 loop 消费。
- **新能力验收**：写一个演示守卫类 + 后台加一条声明 → 生效，全程零核心文件改动（T9 冒烟即此路径）。
- 5b 专项：停用 path_policy 后路径调用不再被拦；`settings_changes` 留痕；恢复默认后拦截恢复。

## 5. 风险与边界

- **5b 是有意的语义放宽**：停用 `path_policy` = 关闭路径管辖；停用 `confirmation` = 关闭确认链。层模式 fail-closed 性质不变（enabled 守卫异常仍拒绝）。UI 后果提示 + 审计兜底，已由用户确认接受。
- `ToolDisabledGuard` 今天注册为**空名单**（`agent_service.py:505` 无参构造 → `_blocked` 为空集，实际不拦任何调用）；迁移保行为（种子仍空名单），其真实名单来源是否需要接线记为计划外待办，不在本方案内擅自接。
- 坏声明运行时策略 = 记日志跳过；保存时校验拦截绝大多数，剩余（如运行环境缺依赖）不炸 build。
- `loop.py` 处于 Pi 迁移关注区——本改动只替换注册来源，不触碰事件/SSE/状态机结构。
- 并行会话同仓库互扰（本地已知坑）：改动涉及 `agent_service.py`/`loop.py`，提交时显式 add。
