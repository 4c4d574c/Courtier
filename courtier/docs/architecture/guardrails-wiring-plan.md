# Guardrails 接线补全与死件清理方案

> 2026-09-04。依据：对 `courtier/agent/core/guardrails/` 的全面审计（会话记录）。
> 审计结论：统一管线落地时删除了 HookChain 与脚手架守卫，但**循环侧的派发
> 没有切换到 `run_scope`**，导致三类词汇里的「适配/记录」整体悬空；另有一批
> 无消费者符号残留。本方案补全接线并清理死件。

## 1. 审计发现（本方案覆盖范围）

### A. 悬空能力（API 在、有测试、生产不生效）

| # | 发现 | 位置 |
|---|------|------|
| A1 | 循环四个检查点直连 `check()`，全仓库无生产代码调 `run_scope` —— 拦截器/观察者注册后**不会执行** | loop.py:375 / 677 / 708 / 810 |
| A2 | `GuardrailSystem.set_context` 从未被调用（循环调的是 `hooks.set_context`，而 `hooks` 恒为 None）—— `_agent_name/_session_id/_context_metadata` 恒空 | loop.py:1159 |
| A3 | `check_call` 用裸 `GuardContext(state=state)` 派发，身份字段永远缺失 | loop_phases.py:413 |

### B. 死代码 / 死符号

| # | 发现 | 位置 |
|---|------|------|
| B1 | HookChain 残迹：TYPE_CHECKING import 指向已删模块、恒 None 的 `hooks` 参数、两处永假派发分支、`set_context` 调用、两处内部转发 | loop.py:48 / 948 / 360 / 912 / 1159 / 1215 / 1263 |
| B2 | `GuardContext.with_field` 零消费 | base.py:139 |
| B3 | `register(replace=True)` 零消费 | guardrail_system.py:110 |
| B4 | `errors.guard_tool_blocked` 模板键无生产者（生产者 DangerousToolGuard 已删） | engine.py:83 / 272、两语言 errors.yaml |
| B5 | 过时注释：`CallGuardResult` docstring 称 confirm「尚无派发侧消费者」（确认链路已消费）；`guardrail_output_layer`/`guardrail_tool_layer` 设置描述缺「当前无内置守卫」说明 | base.py:42、config.py |

**保留不动**（有意决策，见 D3）：`owner` 标签与 `unregister_owner`。

## 2. 目标与非目标

**目标**

1. 循环派发统一切换到 `run_scope`（适配→裁决→记录），拦截器/观察者/超时机制
   真正生效；
2. 守卫/拦截器/观察者在每个 scope 都能拿到 `agent_name`/`session_id`；
3. 删除 §1.B 全部死件；
4. 对用户**零可见行为变化**（平价论证见 §5）。

**非目标**

- 不新增任何内置守卫（input/output/tool 三层继续为空，作为扩展预留面）；
- 不动确认链、域守卫加载、refusal 重试（管线外）、设置模式键（昨日已接线）；
- 不改 `GuardrailSystem` 的对外注册 API 形状（仅删 `replace` 参数，见 D2）。

## 3. 设计决策

### D1（核心）：身份经派发上下文显式传递，不走 `set_context` 共享态

**问题根因**：会话 `GuardrailSystem` 被 orchestrator 与所有子代理**共享**，而
子代理的 `agent_loop` 嵌套在 orchestrator 的工具执行阶段内交错运行。若按
「循环开始时 `set_context(...)`」实现，子代理循环会覆写共享系统上的
agent_name/session_id，控制权交还后 orchestrator 的后续派发带着子代理的
身份——共享可变状态在嵌套场景必然互相污染。

**方案**：身份是每次派发的属性，不是系统的属性。`agent_loop` 把
`agent_name`/`session_id` 显式线程到各阶段函数，每个 `GuardContext` 构造点
就地填充。`run_scope` 的优先级翻转为**上下文优先、系统值兜底**：

```python
# guardrail_system.py run_scope（现 194-195 行，系统值覆盖上下文）
context.agent_name = self._agent_name or context.agent_name
# 改为（上下文优先，系统值仅作扩展兜底）：
context.agent_name = context.agent_name or self._agent_name
context.session_id = context.session_id or self._session_id
```

`set_context` 保留（降级为扩展者的元数据兜底入口，`_context_metadata` 的合并
语义不变——上下文键胜），但平台代码不再调用它。

### D2：删除 `register(replace=True)` 与 `GuardContext.with_field`

零生产消费者、零测试依赖（grep 证实）。运行期守卫的注销走 `unregister`
（按实例身份），`replace` 无场景；`with_field` 是投机便利 API。git 历史可
随时复活。

### D3：保留 `owner` 标签与 `unregister_owner`（标注为域契约预留）

`owner` 有真实生产写入方（`register_domain_guards`，activation.py:326）；
`unregister_owner` 虽暂无调用方，但它是「域停用/热卸载」这一可预期功能的
现成接线点，且与 owner 写入侧构成成对契约。保留，并在 docstring 标注
「当前平台内无调用方——域停用场景的预留」。

### D4：`tool` 层保持条件派发

无工具调用时跳过 `tool` scope 派发（现行为）。空的工具批没有可观察的语义。

## 4. 任务分解

> 每个 T 一个 conventional commit；T1→T3 是核心接线，T4-T5 清理，T6-T7
> 验证与文档。行号基于 2026-09-04 工作区，实施时以符号定位为准。

### T1 `feat(loop)`：四个检查点切换 `run_scope`

**改动文件**：`courtier/agent/core/loop.py`

| 检查点 | 现状 | 改法 |
|--------|------|------|
| 思考前 input | `_run_think_phase` 内 `check("input", GuardContext(state, messages))`（:375） | `outcome = await guardrail_system.run_scope("pre_think", GuardContext(state=..., messages=..., agent_name=agent_name, session_id=session_id))`；`current_state = outcome.state`；`if outcome.blocked:` 走原「转 blocked + break_loop」分支 |
| 输出后 output | `_run_tool_phase` 内（:677） | 同上，scope=`"output"`，context 加 `response_text`/`tool_calls` + 身份；`outcome.state` 采纳；`outcome.blocked` → 原 blocked 分支 |
| 工具批前 tool | `_run_tool_phase` 内（:708），现条件 `if current_state.tool_calls` | scope=`"tool"`，条件保留；blocked 分支同现状 |
| 观察后 post_tool | `_run_tool_phase` 内（:810） | scope=`"post_tool"`，context 保留 `metadata={"artifact_store": ...}` + 身份；**blocked → 转 `completed`**（post_tool 的收尾语义，勿改成 blocked） |

配套签名改动：

- `_run_think_phase` **新增** `agent_name: str`、`session_id: str` 参数
  （现仅有 `hooks`/`guardrail_system` 等）；`agent_loop` 调用点传入；
- `_run_tool_phase` 已有 `agent_name`/`session_id`，只改派发体；
- 后续 metadata 读取（:840 `guard_result.metadata`）改用
  `outcome.guard_result.metadata`，语义不变（同一聚合对象）；
- **采纳 `outcome.state`**：即便当前无拦截器注册，也让状态采纳路径成为常态。

**验收**：全量 `tests/agent` 绿（现有断言逐字节不动）；新增 T5-1 验收测试。

### T2 `feat(loop)`：`check_call` 上下文富化

**改动文件**：`courtier/agent/core/loop_phases.py`、`loop.py`

- `execute_tools_phase(...)` 新增 `agent_name: str = ""`、`session_id: str = ""`
  参数；`_run_tool_phase` 调用点传入；
- :413 改为
  `GuardContext(state=state, agent_name=agent_name, session_id=session_id)`。

**验收**：T5-2。

### T3 `refactor(loop)`：HookChain 残迹删除

**改动文件**：`courtier/agent/core/loop.py`

- 删 `if TYPE_CHECKING: from ..hooks.chain import HookChain`（:48）；
- 删 `agent_loop(hooks: HookChain | None = None, ...)` 参数（:948）及两处内部
  转发（:1215、:1263）；
- 删 `hooks.run("pre_think", ...)` 分支（:360-361）、`hooks.run("post_observe",
  ...)` 分支（:912-913）、`if hooks: hooks.set_context(...)`（:1157-1159）；
- 删 `_run_think_phase`/`_run_tool_phase` 的 `hooks` 参数（T1 已触碰签名）；
- 删空目录 `courtier/agent/hooks/`（仅剩 `__pycache__`）。

测试无 `hooks` 引用（grep 证实），无测试改动。

**验收**：全仓 grep `hooks` 在 `courtier/` 生产代码零命中（guardrail_system.py
模块 docstring 里的历史沿革描述除外）。

### T4 `refactor(guardrails)`：死 API 与死模板清理

**改动文件**：`base.py`、`guardrail_system.py`、`prompts/engine.py`、
`prompts/defaults/{zh-CN,en-US}/errors.yaml`、`config.py`

- 删 `GuardContext.with_field`（base.py:139-150）；
- 删 `register(..., replace: bool = False)` 参数及其过滤逻辑
  （guardrail_system.py:100、110-115），docstring 同步；
- 删 `errors.guard_tool_blocked`：`RESERVED_TEMPLATE_KEYS`（engine.py:83）、
  `FALLBACK_TEMPLATES`（:272）、两语言 errors.yaml 条目；
- 修 `CallGuardResult` docstring：confirm 的消费者是确认链路
  （ConfirmationGuard → loop_phases → RunManager），不再是「尚无」；
- `unregister_owner` docstring 补「当前平台内无调用方，域停用场景预留」（D3）；
- `config.py`：`guardrail_output_layer`/`guardrail_tool_layer` 描述补
  「当前该层无内置守卫（扩展预留）」，与 input 层对齐。

**验收**：`render_error("errors.guard_tool_blocked")` 全仓零引用；
`tests/prompts`（若有键清单断言）同步。

### T5 `test`：接线验收与平价保障

**改动文件**：`tests/agent/test_pipeline.py`、`tests/agent/test_loop.py`、
`tests/agent/core/test_guardrails.py`

1. **接线验收（T1 的存在性证明）**：走真实 `agent_loop`（现有 fake model
   桩），注册 pre_think 拦截器 + post_tool 观察者，断言：拦截器返回的新
   state 被采纳、观察者收到 `GuardContext` 且 `agent_name`/`session_id`
   非空、guard 行为不变；
2. **身份富化**：spy 守卫记录 `context.agent_name/session_id`，orchestrator
   与子代理各跑一轮，断言各自身份正确（嵌套不串，D1 的回归锁）；
3. **check_call 富化**：execute_tools_phase 路径断言 context 身份非空；
4. **优先级翻转**：单测——上下文有值胜、为空时系统值兜底；
5. **平价回归**：现有守卫/路径拒绝/确认链测试零断言改动地保持绿
   （`test_permission_guards.py` 逐字节文案、`test_confirmation.py`、
   `test_guardrail_call_dispatch.py` fail-closed）；
6. 删除项清理：`replace=`/`with_field` 若有残留测试引用一并删除（当前无）。

**跑法**：`uv run pytest tests/agent tests/courtier -q` 全绿。

### T6 `docs`：guardrails.md 与 AGENTS.md 对齐

- §1 流水线图与 §4.6：从「扩展 API 说明」升级为「已接线」的事实描述；
  拦截器/观察者的示例从"怎么写"补上"写了即生效"；
- §5 注册点总览：`set_context` 行改为「循环不调用；身份经每个派发上下文
  显式传递；`set_context` 是扩展者的元数据兜底入口」；
- 补一段：input/output/tool 三层当前无内置守卫，模式键为扩展预留
  （与 T4 的设置描述呼应）；
- AGENTS.md §5.3 guardrails 段落一句带过（派发经 run_scope）。

### T7 真机冒烟（人工，实施完成后）

- 重启后端；跑一次上传文档会话：路径拒绝（`read /etc/hostname`）与
  确认链（配一个 `tool_confirmation`）各触发一次，用户可见结果不变；
- 管理后台「分层模式」改 `guardrail_post_tool_layer=log` 保存，确认
  下一次会话构建生效（影子化强制收尾）——改回 block。

## 5. 行为平价论证（为什么用户零感知）

1. **裁决路径不变**：`run_scope` 内部就是调 `check()`；四个检查点的
   mode→blocked 判定与现状等价——`blocked` 仅在 mode=block 且动作为 block
   时非 None；log 模式下 `check()` 已把 block 降级为 log（现 :277-285），
   旧代码的 `action == "block"` 同样不会触发；off/allow 双方都是整层跳过。
2. **metadata 聚合不变**：`ScopeOutcome.guard_result` 就是 `check()` 的
   返回对象，:840 的计数读取语义一致。
3. **状态采纳是恒等扩展**：无拦截器注册时 `outcome.state` 即原状态。
4. **身份字段是纯新增**：现有守卫不读 `agent_name/session_id`。
5. **唯一的行为面变化是"从死变活"**：拦截器/观察者/超时开始派发——但平台
   内没有任何注册方，Day 1 无人在管线上。

## 6. 风险与回归红线

- **共享系统嵌套**：身份走上下文后无共享可变态；`set_context` 若被扩展者
  在嵌套场景误用属文档责任（docstring 注明「平台代码不调用」）。
- **回归红线**：路径拒绝文案逐字节（`test_permission_guards.py`）、
  fail-closed 契约（`test_guardrail_call_dispatch.py`）、确认三选
  （`test_confirmation.py`）、`OrchestratorAgent.run` kwarg 转发清单
  （本方案不新增 Agent.run 参数，不触发该坑）。
- **执行顺序**：pre_think 上原 `hooks.run` 在 input 检查**之前**；切换后
  拦截器在 `run_scope` 内同样先于守卫（适配→裁决），顺序保持。

## 7. 提交切分

| 提交 | 内容 |
|------|------|
| 1 | T1（run_scope 接线 + 身份线程） |
| 2 | T2（check_call 富化） |
| 3 | T3（hooks 残迹删除） |
| 4 | T4（死 API/死模板） |
| 5 | T5（测试）——1-4 各自的测试随各自提交走，此提交兜底全量回归 |
| 6 | T6（文档） |

T7 冒烟通过后在本文档追加实施记录（含偏差）。
