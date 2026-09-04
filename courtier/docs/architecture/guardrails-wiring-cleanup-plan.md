# Guardrails 接线修复与死代码清理方案

> 2026-09-04。来源：对 `courtier/agent/core/guardrails/` 的全量消费审计（结论见
> 会话记录，要点复制于 §1）。状态：**待批准**。批准后按 T0→T7 逐任务实施，
> 偏离记录在 §8。

## 1. 背景与审计结论

统一管线落地时，`HookChain` 被吸收进 `GuardrailSystem`（interceptor/observer
即原钩子词汇），但**循环侧的接线没有完成**，遗留三类问题：

- **A. 悬空能力**（API + 测试 + 文档齐备，生产路径不生效）：
  - A1 `run_scope`（适配→裁决→记录）无任何生产调用方——循环四个检查点直连
    `check()`（loop.py:375/677/708/810），每调用派发直连 `check_call()`
    （loop_phases.py:410）。后果：interceptor/observer 注册了也不会跑；
    `set_context` 的上下文富化永不生效。
  - A2 `guardrail_system.set_context` 从未被调用——循环调的是 `hooks`
    残迹对象（loop.py:1159），`_agent_name/_session_id/_context_metadata`
    恒空，守卫拿到的 `context.agent_name/session_id` 恒为 `""`。
  - A3 `check_call` 用裸 `GuardContext(state=state)`（loop_phases.py:413），
    即使接了 run_scope，tool_call 层也不富化。
- **B. 死代码/死符号**：HookChain 残迹（loop.py:48/331/665/948/360-361/
  910-913/1157-1159/1215/1263，`hooks` 参数恒 None）、`GuardContext.with_field`
  （零消费）、`register(replace=True)`（零调用）、
  `ExploreLoopGuard.consecutive_exploratory` property（真实通道是 metadata，
  loop.py:840；property 零读者）、`errors.guard_tool_blocked` 模板键（生产者
  DangerousToolGuard 已随脚手架删除）、base.py:42 过时 docstring
  （称 confirm 无派发侧消费者，实际确认链路已消费）。
- **C. 空层与描述缺口**：input/output/tool 三层零内置守卫（脚手架已删），
  三个模式设置键当前为 no-op（扩展预留面）；`guardrail_output_layer`/
  `guardrail_tool_layer` 的字段描述未像 input 一样标注"当前无内置守卫"。

**健康面（本次不动）**：五个内置守卫的注册、确认链闭环、域守卫加载与校验、
`guard.triggered` 事件、`guardrail_blocked_total` 指标、metadata 计数通道、
五层模式设置（60c80ad）均有真实消费。

## 2. 目标与非目标

**目标**：
1. 循环派发改走 `run_scope`，interceptor/observer/上下文富化三项能力真实生效；
2. `set_context` 在循环入口接线，跨子代理运行不误归属；
3. 清除 HookChain 残迹与零消费死符号；
4. 文档与设置描述与实际行为一致。

**非目标**：
- 不改任何守卫的裁决逻辑与文案（逐字节回归红线）；
- 不给 input/output/tool 三层补内置守卫（空层即扩展面，仅修描述）；
- 不做域守卫"注销/停用"功能（见 §4 D4 保留决策）；
- 前端零改动。

## 3. 总体设计

### D1 循环四点改走 `run_scope`

| 检查点 | 现状 | 改后 |
|--------|------|------|
| 思考前（loop.py:~374） | `check("input", ctx)` | `run_scope("pre_think", ctx)` |
| 输出后（loop.py:~676） | `check("output", ctx)` | `run_scope("output", ctx)` |
| 工具批前（loop.py:~707） | `check("tool", ctx)`（仅 `tool_calls` 非空时） | `run_scope("tool", ctx)`（保留非空条件） |
| 观察后（loop.py:~809） | `check("post_tool", ctx)` | `run_scope("post_tool", ctx)` |

统一改写模式：

```python
outcome = await guardrail_system.run_scope("pre_think", GuardContext(...))
current_state = outcome.state                # 拦截器可能已换状态
if outcome.blocked is not None:              # 仅 mode==block 时非 None
    reason = f"guardrail:{outcome.blocked.guard_name}:{outcome.blocked.reason}"
    ...  # 原终态转换不变（input/output/tool → blocked；post_tool → completed）
```

metadata 读取（loop.py:840 的 `consecutive_exploratory`）改从
`outcome.guard_result.metadata` 取。

**语义等价性论证**（回归依据）：
- `blocked` 仅当层模式为 block 且聚合动作为 block 时非 None
  （guardrail_system.py:224）——与现状等价：今天 `check()` 在 log 模式已把
  block 降级为 log（guardrail_system.py:277-285），循环判
  `action == "block"` 天然不触发；
- 每结果 `on_event` 发射路径不变（`check` 内部）；
- 生产当前零 interceptor/observer 注册 → 接线后可观察行为**零变化**，
  行为变化只发生在有人注册时（即修复目的）；
- `pre_think` 拦截器执行位置 = 老 HookChain `pre_think` 钩子位置
  （input 层守卫检查之前，loop.py:360 vs :374），吸收语义保持；
- 老 `post_observe` 钩子由 post_tool scope 的 observer 覆盖（派发点即
  现有 post_tool 检查点，位于 observe 之后），位置相当。

### D2 `set_context` 接线 + 运行期上下文恢复

- `set_context` 改为**返回旧上下文** `dict`（`agent_name/session_id/metadata`），
  现有调用方（将删的 hooks 行之外为零）不受影响；
- 循环入口（替换 loop.py:1157-1159 的 `if hooks: hooks.set_context(...)`）：

  ```python
  prior_context = guardrail_system.set_context(agent_name=agent_name, session_id=session_id)
  ```

  `finally`（与注销 `_run_guards` 同处，loop.py:~1320）：

  ```python
  guardrail_system.set_context(**prior_context)
  ```

- 动机：子代理循环共用同一会话系统并会覆写上下文；finally 恢复保证编排器
  后半程的守卫事件不误归属到子代理名下。同会话内 orchestrator ↔ sub-agent
  为顺序 await，无并发交叠，"last-set-wins + 恢复"足够。

### D3 `check_call` 上下文富化

把 run_scope 的富化段（guardrail_system.py:194-197）抽成私有助手
`_enrich_context(context)`，`run_scope` 与 `check_call`（guardrail_system.py:314）
共用。loop_phases.py:413 的调用方式不变，富化移入系统内部。

### D4 保留决策（审计标"悬空"但不删）

`owner=` 注册参数 + `unregister_owner`：**保留**。理由：二者是配对机制，
`owner=` 在生产使用中（域守卫注册，activation.py:326）；`unregister_owner`
是未来"域停用/管理 UI"功能的对称出口，删掉它等于断掉演进路且让 owner 标签
变成只写不读。每次会话构建新建 GuardrailSystem，当前无注销场景属实，但
机制自洽、有测试、文档已载。§4.4 补一句"当前无注销调用方"。

### D5 删除清单（依据：零生产消费 + 零测试引用，已核实）

| 符号 | 位置 | 依据 |
|------|------|------|
| `hooks` 参数及全部残迹 | loop.py:48/331/665/948/358-361/910-913/1157-1159/1215/1263 | `hooks=` 全仓（含测试）零传值；keyword-only 无位置参数风险 |
| `GuardContext.with_field` | base.py:139 | 零消费（`with_field_report` 为无关子串命中） |
| `register(replace=...)` 参数 | guardrail_system.py:96-115 | 零调用方，文档宣称的"有状态守卫重注册"场景由"每运行 fresh 实例 + unregister"承担 |
| `ExploreLoopGuard.consecutive_exploratory` property | loop_guardrails.py:38-41 | 真实通道是 metadata（loop.py:840），property 零读者 |
| `errors.guard_tool_blocked` 键 | errors.yaml（zh-CN:38/en-US 对应）+ engine.py:83/272 | 生产者 DangerousToolGuard 已删，零渲染方、零测试引用 |
| `courtier/agent/hooks/__pycache__` | 目录 | 幽灵 pyc，防残模块复活 |
| base.py:42 过时 docstring | base.py | confirm 消费链已落地（confirmation.py + loop_phases.py:438） |

## 4. 任务分解

> 每个任务独立提交、独立可回滚；T1/T2 为核心，先做。

- **T0 基线**：`uv run pytest -m "not integration"` 全绿作为基线记录。
- **T1 循环派发改走 run_scope（D1）**
  - 改：loop.py 四个检查点（§3 D1 表）+ loop.py:840 metadata 读取。
  - 验收：循环内直连 `guardrail_system.check(` 归零；`uv run pytest
    tests/agent/test_loop.py tests/agent/core/test_pipeline.py -q` 绿；
    ExploreLoopGuard 强制收尾真机行为不变（靠既有逐字节文案断言兜底）。
- **T2 set_context 接线 + check_call 富化（D2/D3）**
  - 改：guardrail_system.py（set_context 返回旧值 + `_enrich_context` 助手，
    run_scope/check_call 共用）；loop.py 入口 set/finally 恢复。
  - 验收：新增单测——`set_context` 返回旧值；spy 守卫在 `check_call` 中
    读到非空 `agent_name/session_id`。
- **T3 HookChain 残迹清除（D5 第一行）**
  - 改：loop.py 全部 hooks 触点；删 `courtier/agent/hooks/__pycache__`。
  - 验收：全仓 `grep -rn "HookChain\|hooks" courtier/ --include="*.py"` 中
    guardrails 相关命中为零（webhooks 等无关词除外）。
- **T4 死符号清理（D5 其余行）**
  - 改：base.py、guardrail_system.py（register 签名）、loop_guardrails.py、
    errors.yaml ×2、prompts/engine.py。
  - 验收：`uv run pytest tests/agent tests/courtier -q` 绿
    （含 guardrails 聚合/派发/文案逐字节套件）。
- **T5 文档与设置描述（§C + D4 说明）**
  - 改：config.py 两个字段描述补"当前该层无内置守卫，模式暂无效果"；
    docs/guardrails.md §4.6（接线说明改为"注册即生效"，补 tool_call 富化
    说明）、§4.4（补"当前无注销调用方"）、§7（单测落点补新测试）；
    §1/§5 核对（接线后原文从"设计"变"现实"，措辞核对微调）。
  - 验收：文档 grep 与代码一致。
- **T6 全量回归 + 真机冒烟**
  - `uv run pytest -m "not integration"` 全绿；webui 无改动不重测。
  - 真机冒烟：临时注册一个 post_tool observer（debug 日志）跑一次真实
    会话，确认派发到达，验证后移除，不留痕。
  - 重启后端（agent 持有进程，无 --reload）。

## 5. 风险与回归红线

1. **拒绝文案逐字节**：`test_permission_guards.py` 等断言不动、不改写；
   本次零文案变更（`guard_tool_blocked` 是整键删除，无渲染方）。
2. **事件词汇不变**：`guard.triggered` 字段（layer/guard_name/action/reason/
   metadata）由 `_on_guardrail_event` 产出，路径未动。
3. **模式语义不变**：log 影子降级、off/allow 跳过、tool_call fail-closed
   全部在 `check`/`check_call` 内部，run_scope 只是外层编排。
4. **删 `hooks` 参数是 API 变更**：`agent_loop` 全部参数 keyword-only，且
   零调用方传值（tests/agents/api 三处已 grep 核实），同提交内完成无窗口期。
5. **新能力暴露风险**：接线后 interceptor/observer 真实可执行——它们本就是
   文档化扩展点，fail-open/吞异常契约不变；tool_call 层仍拒绝拦截器注册。

## 6. 测试计划

新增（T2/T1 落点，参照 tests/agent/test_loop.py 既有 fake-model harness）：

1. observer 在真实循环 post_tool 派发中被调用一次（session system 上注册）；
2. pre_think interceptor 返回新 state 后，循环后续使用新 state；
3. spy tool_call 守卫断言 `context.agent_name/session_id` 非空（富化生效）；
4. `set_context` 返回旧上下文；finally 恢复后再次读取一致；
5. 既有 test_pipeline.py 的 run_scope 直测保持绿（机制未变）。

回归面：tests/agent/test_loop.py、tests/agent/core/test_guardrails.py、
test_guardrail_call_dispatch.py、test_call_guards.py、test_pipeline.py、
test_permission_guards.py、test_confirmation.py、tests/agent/api/。

## 7. 验收清单

- [ ] 循环四检查点走 run_scope，生产代码直连 `check(` 归零
- [ ] `guardrail_system.set_context` 循环入口被调、finally 恢复
- [ ] `check_call` 的 GuardContext 含 agent_name/session_id
- [ ] HookChain/hooks 全仓归零（含 __pycache__）
- [ ] with_field / replace 参数 / consecutive_exploratory property /
      guard_tool_blocked 键删除，全量测试绿
- [ ] config 字段描述与 guardrails.md 同步
- [ ] 全量 `pytest -m "not integration"` 绿；后端重启健康

## 8. 偏离记录

- T1/T2 无方案偏离。T1 的观察者循环测试在 T2 顺手补了 `agent_name` 全链断言
  （循环 set_context → run_scope 富化 → 观察者读到身份），属测试增强。
- T5 文档同步比方案清单多一处：§2 每层模式补"input/output/tool 三层为扩展
  预留位、暂无内置守卫"的说明（与 config 字段描述同信息）。
- 环境备注（非本方案改动）：`tests/courtier/test_es_backend_integration.py`
  与并行会话并发跑时出现过一次 ES 索引竞争失败，单独复跑即绿。
- 实施提交：T1 842fdc7、T2 b52421d、T3 aa05de6、T4 cb20609、T5 本次。
