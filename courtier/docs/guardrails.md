# Guardrails 统一管线 — 原理、参考与扩展指南

> 对应实现：`courtier/agent/core/guardrails/`。设计来龙去脉见
> `docs/architecture/guardrails-unification-plan.md`；本文回答两个问题：
> **这套系统是什么、怎么工作的**，以及**我想扩展它时该怎么做**。

## 1. 它是什么：一条流水线上的质检站

Agent 每个回合按「思考 → 调工具 → 看结果」循环。GuardrailSystem 在这条流水线的
**5 个固定位置（scope）** 派发检查，每个位置按固定顺序跑三类处理器：

```
思考前(pre_think) → 模型 → 输出后(output) → 工具批前(tool)
    → 【逐个工具调用前(tool_call) → 执行】×N → 观察后(post_tool)
```

三类处理器各司其职，**失败时的行为是写死的，不随配置变化**：

| 词汇 | 干什么 | 出错时 |
|------|--------|--------|
| **守卫 guard** | 裁决：放行 / 记录 / 拦截 | 行为类位置（思考前/输出/工具/观察后）跳过继续；**调用前（tool_call）一律按拒绝处理** |
| **拦截器 interceptor** | 改写：往状态里注入/修改内容 | 跳过继续（fail-open），可设超时 |
| **观察者 observer** | 记录：审计、指标、打点 | 异常静默吞掉 |

这个不对称是刻意的：行为类护栏挂了顶多浪费几轮（有步数上限兜底），
**调用前的权限检查挂了绝不能放行**——fail-open 的权限系统只是日志系统。

两个容易混淆的边界，先划清：

- **工具"看不见"不归它管**——域门控（`DomainActivator`）决定模型能看到哪些工具；
  guardrails 决定**看到了、调过来了，放不放行**。
- **模型拒绝/空响应不归它管**——那属于 think 阶段的重试策略
  （`refusal_*` 设置，见 `refusal-retry-plan.md`），不在 guardrails 派发路径上。

## 2. 三种"拦截"的写法

### 轮级守卫（`check`）

一个层级的所有工具调用打包检查一次。返回 `GuardResult`：

```python
GuardResult.allow(name, metadata={...})   # 放行；metadata 会合并进聚合结果
GuardResult.log(name, reason)             # 只记一条事件，不拦截
GuardResult.block(name, reason)           # 拦下整轮（状态机转终态）
```

### 每调用守卫（`check_call`）

只作用于 `tool_call` 层，**每个工具调用单独裁决**。返回 `CallGuardResult`：

```python
CallGuardResult.allow(name)
CallGuardResult.deny(name, reason, error_code="permission_denied")  # 该调用变错误结果，运行继续
CallGuardResult.confirm(name, message)    # 挂起等用户确认（确认链路，见 §4.3）
```

被拒的调用**不会执行**，但会变成一条错误观察喂回模型（模型可以换路子重试），
同批其余调用不受影响——这叫单调用拒绝。

### 每层模式（off / log / block / allow）

每个层级有一个整体模式，在**构造 `GuardrailSystem` 时定死**——当前装配没有
运行时开关：

- `block`：裁决生效。会话系统的 `tool` / `tool_call` 固定用它（权限层必须 fail-closed）；
- `log`：**影子模式**——照常跑检查、只发事件不拦截。新规则先影子观察一段时间
  再切 block，是推荐的上线方式；
- `off` / `allow`：整层跳过。

装配现状：`build_agent` 的会话系统传 `tool`/`tool_call` = `block`，其余层用
构造默认（`input`/`post_tool` = `block`，`output` = `log`）；扩展者自建
`GuardrailSystem` 时可按层传模式参数。

> **别找管理后台开关**：`Settings.guardrails`（`GuardrailsConfig` 的四层模式
> 字段）目前没有任何消费者——统一管线落地前的遗留平行定义，改它不生效。
> 要调模式只能改构造处传参（`agent_service.py` / `loop.py`）。

### metadata 怎么流动（容易踩的点）

聚合结果的动作/署名取**最严格**的（allow < log < block），
但 **metadata 是全部结果按执行顺序合并的**（同名键后者覆盖）。
所以守卫想往外传状态（比如"连续空转计数"），放 `allow` 的 metadata 即可，
循环侧从聚合结果的 `metadata` 里读。

## 3. 内置了什么

| 守卫 | 位置 | 干什么 | 归谁配置 |
|------|------|--------|----------|
| `ToolDisabledGuard` | tool_call | 名单内工具整体禁用 | 代码注册（生产默认空名单） |
| `PathPolicyGuard` | tool_call | 文件路径白名单：`read`/`edit`/`write` 的路径解析后必须落在会话根（会话工作区；记忆已 DB 化走 memory 插件工具，不再受路径管辖）内 | `build_agent` 按会话注入 |
| `ConfirmationGuard` | tool_call | 名单内工具挂起等用户确认 | 管理后台 `tool_confirmation` 名单 |
| `ExploreLoopGuard` | post_tool | 连续空结果 / 重复调用 / 空转 → 强制收尾 | `loop_*` 设置 |
| `BusinessArtifactProgressGuard` | post_tool | 连续无业务产出 → 强制收尾 | `loop_*` 设置 |

前三个是**无状态**的，由 `build_agent` 构造后 orchestrator 和所有子代理共享；
后两个是**有状态**的（要记历史），由 agent 循环**每轮运行期注册、结束注销**——
这两类生命周期不能混，见 §8 坑 3。

## 4. 扩展场景逐个讲

> 每个场景按固定结构：**怎么写 → 放在哪里 → 和谁接线 → 注意什么**。
> 先看总览，按目的对号入座：

| 我想…… | 去哪一节 | 要不要改代码 |
|--------|----------|--------------|
| 拦截/检查整轮行为（输出敏感词、注入提醒前检查等） | §4.1 | 要（新守卫类） |
| 控制某个工具调用能不能执行（参数级规则） | §4.2 | 要（新守卫类） |
| 让某工具执行前必须用户点头 | §4.3 | **不要**（纯配置） |
| 给域包加专属策略检查 | §4.4 | 要（guard 类 + yaml 一行） |
| 让自定义工具受路径白名单管 | §4.5 | 要（注册一条 Capability） |
| 在生命周期点改状态 / 做审计 | §4.6 | 要（拦截器/观察者） |
| 加一个给模型看的错误/提示文案 | §4.7 | 要（模板键） |

### 4.1 加一个轮级行为守卫

**场景举例**：输出层检查模型回复里是否泄露了内部提示词；工具批前检查调用总
大小是否超限。

**怎么写**（完整骨架）：

```python
# courtier/agent/core/guardrails/my_guard.py
from courtier.agent.core.guardrails import GuardContext, GuardResult


class MyOutputGuard:
    name = "my_output"      # 必填：聚合署名、事件、日志都用它
    layer = "output"        # 必填："input" | "output" | "tool" | "post_tool"
                            # （"tool_call" 属于 §4.2，这里不要用）

    async def check(self, context: GuardContext) -> GuardResult:
        text = context.response_text or ""
        if "内部提示词" in text:
            # 拦截：本轮转终态，reason 会展示/落审计
            return GuardResult.block(self.name, "回复包含内部信息，已拦截")
        # 想给循环传状态：放 allow 的 metadata（会合并进聚合结果）
        return GuardResult.allow(self.name, metadata={"checked": len(text)})
```

**放在哪里**：`courtier/agent/core/guardrails/` 下新建模块（如 `my_guard.py`），
并在同目录 `__init__.py` 导出。

**怎么注册（关键，二选一）**：

- **无状态守卫**（每次 check 独立）→ 加进 `agent_service.py` 里 `build_agent`
  的 `session_guardrails` 注册序列，对所有会话 + 子代理生效；
- **有状态守卫**（类里存历史/计数）→ 必须在 `loop.py` 的 `_run_guards` 列表里
  注册（运行期注册、结束自动注销）。直接塞进会话系统会和其它代理混用历史，
  这是事故源，见 §8 坑 3。

**和其他模块的接线**：

- **配置项**：阈值之类放 `config.py` 的 `Settings`（字段名用 `loop_` / `refusal_`
  前缀或加入 `_setting_category` 的 guards 组），自动出现在管理后台
  「运行守卫与预算」且热生效；守卫内 `get_settings()` 读取（参考
  `loop_guardrails.py`）。
- **前端可见**：`GuardResult` 任何动作都会自动发 `guard_triggered` 事件 →
  时间线里出现一条守卫提示（`GuardMessage`），无需前端改动。
- **指标**：block 动作自动计 `guardrail_blocked_total{layer, guard_name}`。

**注意什么**：

- `check` 抛异常 = 该守卫本轮被跳过（fail-open）。别在里面做网络请求这类
  不可靠操作；抛了异常的守卫等于不存在。
- `reason` 会在拦截时展示给模型/用户，写清楚"发生了什么 + 建议怎么办"。
- 改**既有**守卫的拦截文案属于回归红线（测试逐字节断言），新增随意。

### 4.2 加一个每调用权限守卫

**场景举例**：禁止对同一工具的并发调用；按参数组合拒绝某类调用；
按会话来源放行/拒绝。

**怎么写**：

```python
# courtier/agent/core/guardrails/my_call_guard.py
from courtier.agent.core.guardrails import CallGuardResult


class MyCallGuard:
    name = "my_call"
    layer = "tool_call"     # 固定这个值

    async def check_call(self, call, context) -> CallGuardResult:
        # call: ToolCall（.name / .arguments / .id）
        if call.name == "search" and not call.arguments.get("query"):
            return CallGuardResult.deny(
                self.name,
                "search 需要非空 query 参数，请补全后重试",
            )
        return CallGuardResult.allow(self.name)
```

**放在哪里**：同 §4.1（`guardrails/` 新模块 + 导出），注册进 `build_agent`
的会话系统（tool_call 层守卫都是无状态的，放心共享）。

**和其他模块的接线**：

- **审计**：被拒调用自动进 `ToolExecutionRecord`（`result_error` = 你的 reason）
  和 SSE 工具卡（红色错误态）——无需额外接线。
- **每个子代理同样受限**：会话系统共享给子代理；它们没有确认通道
  （见 §4.3 注意事项），返回 `confirm` 会直接按拒绝处理。
- **配置注入**：需要会话级差异（如按 owner 放行）时，在 `build_agent` 里
  构造守卫时传参——参考 `PathPolicyGuard(allowed_roots=[...])` 的做法。

**注意什么**：

- **纯内存判定是硬约束**：无网络请求、无文件读写、无数据库。该层 fail-closed
  ——守卫抛异常时调用会被**拒绝**（`error_code: permission_error`），
  一个不可靠的检查等于不停拒绝用户。
- **拒绝文案是模型可见的**：它是喂回模型的错误观察。写"什么被拒 + 为什么 +
  模型该怎么办"；需要复用既有文案走 `render_error("errors.xxx", ...)`（见 §4.7）。
- 派发顺序即注册顺序，**首个 deny 生效即短路**。权限类守卫放在确认类之前，
  保证"越界路径"直接被拒而不是弹确认。
- **不要**返回 `confirm`，除非你接了确认链路（§4.3）——没有 handler 时它会被
  当拒绝处理。

### 4.3 让某个工具执行前必须用户确认（不改代码）

**场景举例**：`write` 写文件前让用户点头；某个高成本插件调用要审批。

**怎么做（纯配置）**：管理后台 → 运行守卫与预算 → `tool_confirmation`，填：

```json
[{"tool": "write", "message": "写操作需要你确认后才会执行"}]
```

保存即热生效（下一次该工具调用时挂起）。用户会看到三种裁决：
**仅本次执行 / 本会话内放行 / 拒绝**。

**背后自动发生了什么**（都已实现，无需接线）：

- `build_agent` 检测名单非空 → 注册 `ConfirmationGuard`；
- 该工具被调用时运行**挂起**（RunManager 持有一个待决 Future），SSE 发
  `confirmation_requested`；当前标签页弹出确认卡（输入框顶部分区）；
- 其他标签页的历史列表该会话行显示「待确认」徽标（NotificationHub 推送）；
- 裁决走 `POST /api/sessions/{id}/confirmations/{cid}`；「本会话内放行」
  持久化到会话（`approvedTools`），重开页面也不再询问；
- 「拒绝」给模型一条 `confirmation_denied` 错误观察，模型自己调整。

**注意什么**：

- **无自动超时**：挂起的运行一直占用户并发额度，用户可点停止；
- **子代理没有确认通道**：子代理里调用名单内工具会被直接拒绝
  （`confirmation_denied`）——名单里只放主流程会用的工具；
- 名单是**工具名级**的，不做参数级条件（参数级的事归 §4.2 / §4.5）；
- 输入必须是**合法 JSON**（键名带双引号）。

### 4.4 给域包加专属守卫

**场景举例**：docaudit 想加一条"查重前必须先解析"的调用顺序检查。

**怎么写**：§4.1 / §4.2 的标准守卫类，但**放在可导入的模块里**——
声明是类路径（`"<module>.<Class>"`），加载靠 `importlib`。当前约束：
域包目录本身不是 Python 包，所以类要么放在已安装的包里
（测试先例：`courtier/agent/testing/domain_guard.py` 的 `DummyDomainGuard`），
要么随域包分发可安装的 Python 包后用其导入路径。

**放在哪里（声明）**：域包 `config/domain.yaml`：

```yaml
guards:
  - docaudit.guards.FormatGuard
```

**怎么接线**：

- `courtier validate-domain domains/<name>/` 会校验：类可导入、有 `name`、
  `layer` 合法——写完先跑一遍；
- 域激活时（包括会话重开后的自动重放）由 `DomainActivator` 注册进会话系统，
  归属 `domain:<名字>`；`unregister_owner` 可对称注销；
- 加载失败**只告警跳过**，不会阻断域激活——但 validate-domain 会提前拦住。

**注意什么**：

- tool_call 层的域守卫同样受"纯内存"约束；
- 机制本身有测试 dummy（`tests/agent/test_domain_guards.py`），目前
  docaudit 尚无真实域守卫——你写的会是第一个，记得补一个真名守卫的用例。

### 4.5 让自定义工具受路径白名单管

**先说清它在解决什么问题。** `PathPolicyGuard` 拦截一个文件类工具调用时，
第一件事是回答："**这个工具，允许它读写哪些路径？**"

- 内置的 `read`/`edit`/`write` 是**平台基线**：始终受管，范围 = 会话根
  （会话工作区；原记忆目录根已随记忆 DB 化退役，2026-09-04），由 `build_agent` 注入——不需要任何声明；
- **自定义工具**：默认不受管（可以碰磁盘任何位置——通常这正是漏洞），
  需要在工具类上**声明自己的路径**，声明了就恰好只有声明的那些。

**声明即全部**：写明哪些路径，工具就恰好只有那些路径，没有任何隐式默认、
没有"会话根打底"。

**怎么写**（工具类上一个属性，和 `name` 放在一起，忘不掉）：

```python
class ExportReportTool:
    name = "export_report"
    path_policy = {"path": ["/srv/reports", "~/data/exports"]}
    # 该工具只能读写这两个目录（支持 ~ 展开）；其余一律拒绝
```

声明不完整（带了权限字典却没有 `path` 键）在**播种时抛 ValueError**（fail
loud）；守卫侧遇到看不懂的声明按 **fail-closed** 处理（该工具全部路径拒绝）。
两条路都不给"静默裸奔"留门。

**放在哪里**：不需要任何注册调用。`build_agent` 每次构建会话时扫描会话
工具表（`permission_guards.declare_path_policy_tools`），凡自带
`path_policy` 声明的工具自动注册名片；注册表实例同时交给 `PathPolicyGuard`
和 `AgentRuntime`。新增受管工具 = 写工具类时多一行属性，任何接线代码
（含 agent_service.py）都不用改。

**管理后台覆盖（无需改代码的管理方式）**：管理后台 → 运行守卫与预算 →
「确认与路径策略」→ **工具路径白名单**——按工具的行编辑器（非 JSON 手写）：

- 按工具分行编辑：工具名 + **允许的路径（每行一条，可单独增删）** +「豁免」勾选；工具名输入框带
  **已知工具下拉补全**（内置 + 插件工具，来自
  `GET /api/admin/settings/tool-names`）；
- 列出的工具**恰好只能读写这些路径，且优先于工具类内的代码声明**——
  管理员可以给没做声明的工具（含新增的插件工具）补上管辖，
  也可以收窄/豁免已声明的工具；
- `false` 行为 = 显式豁免（连类内声明和老名单基线一并遮蔽）；
- 未列出的工具按代码声明或基线；
- 热生效（下一次会话构建即用新值）；声明不完整的行保存时会被校验拦下。

**两条安全语义**：声明值在播种时由 `declare_path_policy_tools` 解析成具体根
（`~` 展开；会话相关路径在工具类里写不了的，就用设置项给具体路径）；解析
失败在播种时抛 ValueError（fail loud），守卫侧遇到看不懂/不完整的声明按
**fail-closed** 处理（该工具全部路径拒绝，显式豁免除外）——两条路都不给
"静默裸奔"留门。

**注意什么**：

- 声明与设置里的路径都建议绝对路径或 `~` 开头；相对路径按服务进程
  工作目录解析，不建议依赖；
- 一次声明一组路径，**不支持组合**（比如"会话根 + 某个额外目录"表达不了
  ——这种需求目前需要在会话接线处注册具体根，属于显式特例）；
- **替代效应**：给某工具放宽的根，模型就能借它触达放宽区域。每工具的根
  应当**等于或窄于**会话基线，放宽前想清楚；
- 内置三件套不写 `path_policy` 属性——它们由老名单基线管（见上），
  写了属性反而会脱离会话根、变成只认自己声明的路径。

### 4.6 在生命周期点改状态 / 做记录

**场景举例**：思考前给状态注入一段提示；每次工具批结束后打一条审计点。

**怎么写**：

```python
async def my_observer(context: GuardContext):
    # context: state / tool_calls / tool_results / agent_name / session_id ...
    ...  # 记录、上报；异常自动吞，随便写

async def my_interceptor(context: GuardContext):
    state = context.state
    ...  # 检查/修改状态
    return new_state   # 返回 None 表示不修改
```

**放在哪里 + 怎么接线**：在持有 `GuardrailSystem` 实例的代码里注册——
通常是 `build_agent` 构造会话系统之后，或运行期拿到
`agent.guardrail_system`：

```python
system.register_observer("post_tool", my_observer)
system.register_interceptor("pre_think", my_interceptor, priority=10, timeout=2.0)
```

**注意什么**：

- 拦截器**恒 fail-open**（异常跳过）、按 priority 降序执行；
- `tool_call` 拒绝注册拦截器（它必须保持裁决独占，防止扩展点改写/注入调用）；
- 生命周期点只需要**观察**（不需要在派发路径上）时，优先考虑 EventBus 订阅
  而不是 observer——两者区别：观察者在管线内、同步、能拿到 GuardContext；
  EventBus 是异步扇出（SSE/遥测），拿不到返回值。

### 4.7 加模型可见的错误/提示文案

守卫的 `reason`（拒绝原因）和 `CallGuardResult.deny` 的文案都是**喂给模型的
观察内容**。约定：

1. 文案定义进 `courtier/prompts/defaults/{zh-CN,en-US}/errors.yaml`（双语都要）；
2. 在 `prompts/engine.py` 的 `RESERVED_TEMPLATE_KEYS` 和 `FALLBACK_TEMPLATES`
   各注册同名键（否则引擎忽略该键）；
3. 代码里 `render_error("errors.your_key", param=value)` 渲染（自动按 locale）。

**注意**：修改**既有**守卫文案（如路径白名单的"不在允许范围内"）属于回归
红线——测试逐字节断言，且用户已按旧文案形成预期。新增键随意。

## 5. 注册点总览（接线地图）

| 时机 | 谁 | 注册什么 |
|------|-----|----------|
| 每会话构建（`build_agent`） | 平台 | ToolDisabledGuard、PathPolicyGuard（会话根）、ConfirmationGuard（名单非空时） |
| 每轮运行（`agent_loop` 进入/退出） | 平台 | ExploreLoopGuard、BusinessArtifactProgressGuard（临时注册/注销） |
| 域激活（`DomainActivator`） | 域包（yaml 声明） | 域守卫（owner = `domain:<名字>`） |
| 运行期（你的代码） | 扩展者 | 拦截器 / 观察者 / 追加守卫 |

注册 API 一览（`GuardrailSystem`）：

```python
system.register(guard)                          # 轮级/每调用守卫
system.register(guard, owner="domain:x")        # 带 owner，可 unregister_owner 撤销
system.register_interceptor(scope, handler, priority=0, timeout=None)
system.register_observer(scope, handler)
system.set_context(agent_name=..., session_id=...)   # 循环自动调用
```

## 6. 设置与运维

相关设置键（管理后台 → 运行守卫与预算，均热生效）：

| 键 | 作用 |
|----|------|
| `tool_confirmation` | 确认名单（JSON `[{tool, message}]`），见 §3 与 confirmation-interaction-plan |
| `tool_path_policies` | 按工具覆盖路径白名单（§4.5 管理后台覆盖层） |
| `loop_*` / `subagent_*` / `run_*` / `max_runs_per_user` / `max_total_runs` | 行为护栏与运行预算阈值 |

`refusal_*` 三键属于模型行为恢复策略（think 阶段，管线外），但同页可配。

## 7. 调试与测试

- **看触发了什么**：总线事件 `guard.triggered`（含 layer/guard_name/action/reason）
  会进会话时间线；服务端指标 `guardrail_blocked_total{layer, guard_name}`。
- **确认链路排查**：会话详情 `pendingConfirmations` 字段 + SSE
  `confirmation_requested/resolved`；挂起时 `/api/stop` 仍可终止。
- **单测落点**：聚合语义 `tests/agent/core/test_guardrails.py`；每调用词汇
  （CallGuardResult/CallGuardrail）`test_call_guards.py`；每调用派发与
  fail-closed `test_guardrail_call_dispatch.py`；路径策略（文案逐字节）
  `test_permission_guards.py`；域机制 `test_domain_guards.py`；确认链路
  `test_confirmation.py`。跑法：`uv run pytest tests/agent -q`。
- **真机验证**：配 `tool_confirmation` 触发一次确认；用 `read` 读
  `/etc/hostname` 触发一次路径拒绝——两者都有稳定可断言的用户可见结果。

- **配套前端表格编辑器**：为设置项做"按行增删"的结构化编辑时，复用
  SystemSettings.vue 的统一表格样式（`.tbl-*` 体系）：`.tbl-editor` +
  修饰类提供 `--tbl-cols` 列宽，表头 `.tbl-head`、行 `.tbl-row`、输入
  `.tbl-input`、行删除 `.tbl-remove`、底部添加 `.tbl-add`；工具名建议
  面板用 `.tbl-toolcell / .tbl-suggest / .tbl-suggest-item`。现成参照：
  插件端点映射、工具确认名单、工具路径白名单。

## 8. 已知坑（都是踩过的）

1. **`OrchestratorAgent.run` 覆写必须同步转发新 kwarg**。给 `Agent.run` 加参数
   后只改基类，所有 API 运行会在启动调用处 TypeError（`confirmation_handler`、
   `media_parts` 各踩过一次，单测覆盖不到 runner 路径）。加参数时把 orch 覆写
   列入清单。
2. **有状态守卫塞进会话系统 = 跨代理历史污染**。会话系统被 orchestrator 和
   子代理共享，有状态守卫必须走 `agent_loop` 的运行期注册/注销。
3. **聚合 metadata 合并但动作严格度独立**：全 allow 时聚合的动作/署名仍是
   系统占位（`guardrail_system`），只有 metadata 保留各守卫的贡献。
4. **拒绝文案逐字节**是回归红线：改文案前先搜
   `tests/agent/test_permission_guards.py` 里的断言。
