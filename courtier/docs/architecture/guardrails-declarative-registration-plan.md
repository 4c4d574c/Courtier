# 守卫声明化注册方案（Guardrails Declarative Registration）

> 2026-09-04 · 状态：已实施（T0-T8 落地，T9 冒烟部分完成，见文末实施记录）
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
| 6 | ToolDisabledGuard 名单来源 | **新 settings 键 `tools_disabled`**（字符串列表），`build(ctx)` 读取；admin 候选项复用现成 `GET /api/admin/settings/tool-names`（详见 §2.7） |
| 7 | `unregister_owner` 处置 | **确认死代码，删除**。`active_domains` 只增不减、全库无 deactivate 路径、符号零消费者；与 cb20609（drop zero-consumer symbols）同一处置标准。`register_domain_guards` 的 `owner` 形参保留为纯日志用途 |

记录在案的现状勘误（随本方案修正文档）：`guardrails.md` §4.1 引用"§8 坑 3"实为坑 2。

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

- **`agent_service.py`**：删三个硬编码 `register`；改为读 `guardrail_guards` → 过滤 `enabled && scope=="session"` → 按列表顺序注册。`GuardSessionContext` 就地构造。`declare_path_policy_tools`（capability 侧声明扫描）不动。注册来源（内置/声明）只进日志行，不落 owner 存储（决策 7：owner 机制删除）。
- **`loop.py`**：`GuardrailSystem` 增加字段 `run_descriptors: list[GuardDescriptor] = []`（默认空，所有现存构造点向后兼容）；`build_agent` 把 run 级已启用描述符附加到系统上；`loop.py:1134` 的硬编码列表换成遍历 `run_descriptors` 逐条实例化，finally 注销（`loop.py:1303`）逻辑不变。**有状态语义逐字节等价**：配方 → 每 `agent_loop`（编排器轮、每个子代理轮）新实例 → 用完丢弃，§8 坑 2 的跨代理历史污染防线原样保留。
- `ExploreLoopGuard` 被 loop.py 消费的 metadata（`consecutive_exploratory`，`loop.py:827`）路径不动；被停用时 `.get` 缺省路径已安全（T4 加测试确认）。
- `ConfirmationGuard` 行为等价性：现状是 `tool_confirmation` 非空才注册；迁移后常驻注册 + 空规则自然 inert（`check_call` 无规则命中返回 allow），T5 用测试确认两者等价。

### 2.4 域通道统一（收尾任务，可独立裁剪）

`domain.yaml` 的 `guards:` 继续接受字符串（= session 级无参，现状语义），**新增接受对象形式**（含 `scope: run`）。run 级描述符落 `system.run_descriptors`，随每 run 重建自然回放。域通道暂不开放 `build(ctx)` 工厂（域守卫保持无参构造；需要会话上下文的域场景目前不存在，记录为边界）。此项关闭"域包声明有状态守卫会踩坑 2"的现存隐患。

### 2.7 ToolDisabledGuard 名单来源（决策 6）

现状：`agent_service.py:505` 无参注册 → `_blocked` 为空集，守卫实际不拦任何调用，纯预留位。设计如下：

- **新 settings 键 `tools_disabled: list[str]`**（guards 类「运行守卫与预算」），与 `tool_confirmation`（守卫读的配置）、`refusal_patterns` 同款模式：守卫类读 settings，配置与管理入口分离于守卫本身。
- **`ToolDisabledGuard.build(ctx)`**：`return cls(blocked=ctx.settings.tools_disabled)`——成为 `build(ctx)` 工厂协议的第一个真实用户。
- **保存校验**（config.py field_validator）：非空字符串列表、去重；**不做工具名存在性校验**——禁用名单是黑名单语义，拼错的项只是永不命中、无害（fail-open）；且工具名随域/插件激活而变，存在性校验会误伤"预先禁用未激活域的工具"这一合理用法。对比：`tool_path_policies` 走 registry 严校验，因为它管辖的必须是确切工具。
- **admin UI**：工具名标签编辑器，候选项来自现成端点 `GET /api/admin/settings/tool-names`（admin_settings.py:202，含懒注册的 read/edit/write；路径白名单编辑器的同一数据源）。
- **生效语义**：被禁工具仍在模型工具表中——模型知道它存在，调用时收到模型可见的"工具 X 已被禁用，请改用其它工具或直接给出文本回答"。这是它区别于"注册期过滤"的价值：教学性反馈、防幻觉重试（注册期过滤是另一种可选机制，本方案不做，两者不互斥）。
- **会话一致性**：会话系统共享给子代理 → 同一名单全局生效，与权限基线语义一致。
- **与 5b 的交互**：停用 `tool_disabled` 守卫条目 = 名单整体失效，走审计兜底。

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
| T0 | 删除零消费者 owner 机制（决策 7）：`GuardrailSystem.register(owner=)` 参数、`_owners`、`unregister_owner`；`register_domain_guards` 的 `owner` 形参保留为纯日志；域/activation 调用点同步 | `guardrail_system.py`、`domain_guards.py`、`activation.py` |
| T1 | registry 统一加载器（Descriptor、GuardSessionContext、工厂协议、合法性校验）+ 单测 | `guardrails/registry.py`（新）、`domain_guards.py` 收敛 |
| T2 | settings 键 `guardrail_guards` + `tools_disabled` + 启动播种 + 两者保存校验 + 单测 | `config.py`、启动迁移、admin 校验错误 |
| T3 | 五内置增加 `build()` 工厂（ToolDisabledGuard 读 `tools_disabled`；ConfirmationGuard 空规则 inert 等价确认） | `permission_guards.py`、`confirmation.py`、`loop_guardrails.py` |
| T4 | agent_service 会话根改造（删硬编码、遍历声明、ctx 构造）+ 等价测试（默认全开=现状；停用=不注册；tools_disabled 生效） | `agent_service.py` |
| T5 | loop.py run 级改造（`run_descriptors` 字段、遍历实例化、finally 注销、metadata 缺省测试） | `guardrail_system.py`、`loop.py` |
| T6 | 前端：守卫声明编辑器 + `tools_disabled` 工具名编辑器（候选来自 `/api/admin/settings/tool-names`）+ 前端测试 | `SystemSettings.vue`、`settingsForm.ts`、`settingsLabels.ts`、`admin.css` |
| T7 | 域通道统一（对象形式、run 描述符落点）+ 域测试 | `domain/loader.py`、`activation.py`、`registry.py` |
| T8 | 文档同步：guardrails.md（§4.1/§4.2/§4.4 注册故事重写、§4.3 旁新增"禁用工具"场景、§5 接线地图、§8 坑 2 补域通道、坑编号勘误）、根 AGENTS.md §5.3、`docs/operations/settings-and-seeds.md`（新键） | docs |
| T9 | 真机冒烟（按用户流程）：后台新增自定义 session 级 + run 级声明各一条验证生效；`tools_disabled` 禁用一个工具验证模型可见拒绝；停用 path_policy 验证 5b 可停用 + 审计落 `settings_changes`；「恢复默认」；真实会话日志核对 `guard.triggered` 事件与指标标签 | 运行环境 |

依赖关系：T0 独立可先行；T1 → T3 → (T4, T5) → T6/T7 → T8/T9。T4、T5 之间无依赖可并行推进。

## 4. 测试与验收

- `uv run pytest -m "not integration"` 全绿；`cd courtier/webui && npm test` 全绿。
- **行为等价清单**（默认五内置全开时与现状逐字节对齐）：tool_call 派生顺序（ToolDisabled → PathPolicy → Confirmation）、拒绝文案不动（回归红线，`tests/agent/test_permission_guards.py` 逐字节断言）、ConfirmationGuard 空规则 inert、`guard.triggered` 事件、`guardrail_blocked_total` 指标标签、ExploreLoopGuard metadata 聚合与 loop 消费。
- **新能力验收**：写一个演示守卫类 + 后台加一条声明 → 生效，全程零核心文件改动（T9 冒烟即此路径）。
- 5b 专项：停用 path_policy 后路径调用不再被拦；`settings_changes` 留痕；恢复默认后拦截恢复。

## 5. 风险与边界

- **5b 是有意的语义放宽**：停用 `path_policy` = 关闭路径管辖；停用 `confirmation` = 关闭确认链。层模式 fail-closed 性质不变（enabled 守卫异常仍拒绝）。UI 后果提示 + 审计兜底，已由用户确认接受。
- `ToolDisabledGuard` 名单来源已设计（§2.7，决策 6）：迁移前为空名单预留位，迁移后由 `tools_disabled` 驱动，种子默认空列表（迁移本身零行为变化）。
- 坏声明运行时策略 = 记日志跳过；保存时校验拦截绝大多数，剩余（如运行环境缺依赖）不炸 build。
- `loop.py` 处于 Pi 迁移关注区——本改动只替换注册来源，不触碰事件/SSE/状态机结构。
- 并行会话同仓库互扰（本地已知坑）：改动涉及 `agent_service.py`/`loop.py`，提交时显式 add。

## 6. 实施记录（2026-09-04）

提交序列：bb6e211 (T0) → ada016e (T1) → 2d4df07 (T3) → b8efc8d (T2) →
24b4c5f (T4) → 735e3c7 (T5) → 4a63ead (T7) → d164e53 (T6) → 31595a9 (T8)。
全量测试 2302 passed / webui npm test 全绿。

**与计划的偏差**：

1. **T3 先于 T2**：T2 的保存校验需深校验种子条目，ConfirmationGuard 必须
   先有 `build()`（否则必填参数在静态检查不过）。
2. **播种双层化（设计增强）**：除启动播种外，`guardrail_guards` 的**字段
   默认值**即五个内置（`config.default_guard_declarations()`）——env-only
   模式（无 DB）与"admin 删除原始键"场景下基线仍然在场；DB 值（含清空
   列表）总是覆盖默认。启动播种仍保留，供后台编辑真实行 + 审计起点。
3. **装配逻辑抽为 `registry.wire_guard_declarations()`**：build_agent 内联
   逻辑改为可单测函数（test_guard_wiring.py 直测等价契约）。
4. **`_json_safe` 递归化**：settings_store 的 JSON 安全化原本只处理裸
   BaseModel，列表/字典内的模型实例（GuardDeclaration）会炸 JSON 列——
   已改为递归处理（batch 跑测试时暴露）。
5. **前端「恢复默认」= 前端内置种子常量**（`DEFAULT_GUARD_ROWS`）：与服务
   端种子重复一份；身份字段保存时服务端权威校正，漂移只影响初值展示。
6. **T9 冒烟部分完成**：✅ 启动播种（audit id48 `system-guard-seed`）、
   ✅ 坏声明 422 拦截（错误含具体导入失败原因）、✅ 5b 停用 path_policy +
   借壳 class_path 被服务端强制回种子 + 审计留痕（id49/50）+ 恢复、
   ✅ 自定义 session（工厂类）/run（有状态类）声明各一条保存持久化 +
   清理。❌ 未做：真实 LLM 会话 run 验证（guard.triggered 事件、
   tools_disabled 模型可见拒绝）——当时 LLM 端点不可达，待端点恢复后
   按用户流程以真实会话日志补验。
