# 代码审查报告

## 基本信息

- **审查日期**：2026-07-17
- **审查模块**：Agent 核心引擎（`courtier/courtier/agent/core/`）
- **审查范围**（主审 5 文件 + 交叉依赖 8 文件）：
  - 主审：`core/loop.py`、`core/model.py`、`core/state.py`、`core/event_bus.py`、`core/events.py`
  - 交叉：`core/loop_phases.py`、`core/loop_hints.py`、`core/loop_guards.py`、`core/state_machine.py`、`core/streaming.py`、`agents/base.py`（调用方）、`api/sse_adapter.py`（事件消费方）、`api/services/stream_service.py`（流生命周期）
- **代码版本**：HEAD `4acd4e1` + 工作区未提交改动（含 `core/loop.py`、`core/model.py` 等；审查基于工作区实况）
- **审查人**：Kimi Code（AI agent）
- **总耗时**：约 30 分钟
- **环境检查**：
  - ⚠️ Lint 门未过：`ruff check courtier/agent/core/` 有 4 个错误（`audit_logger.py` I001 + E501:212；`loop_guards.py` I001 + E501:38，其中 2 个可 `--fix`）
  - ✅ 测试：`tests/agent/test_loop.py + test_model.py + test_state.py` 共 46 项全部通过（未统计覆盖率）
  - ⚠️ 工作区存在未提交改动，本次审查非固定 commit

---

## 文件概览

| 文件路径 | 总行数 | 函数/方法数 | 问题数 🔴🟡🟢 | 整体评价 | 备注 |
|---------|-------|-----------|--------------|---------|------|
| `core/loop.py` | 922 | 6 + 7 闭包 | 1/4/2 | 需重构 | 错误路径事件缺失；指标双计 |
| `core/model.py` | 704 | 14 | 0/1/3 | 清晰 | JSON 修复启发式脆弱；有死代码 |
| `core/state.py` | 358 | 12 | 1/0/1 | 清晰 | max_steps 截断产生残缺历史 |
| `core/event_bus.py` | 148 | 8 | 0/0/1 | 清晰 | 背压策略完备 |
| `core/events.py` | 55 | 1 | 0/0/1 | 清晰 | docstring 过度承诺 |
| `core/loop_phases.py` | 297 | 2 | 0/2/1 | 清晰 | 访问模型私有属性 |
| `core/loop_hints.py` | 276 | 7 | 0/2/0 | 需重构 | 绕过状态机；提示重复注入 |
| `core/loop_guards.py` | 180 | 6 | 0/0/1 | 需清理 | ~120 行死代码 |
| `core/state_machine.py` | 140 | 5 | 0/1/0 | 清晰 | 转移表缺 `blocked` 终态 |
| 跨文件（loop↔api 层） | — | — | 1/1/0 | 高风险 | 模型错误被静默为"正常完成" |

---

## 详细审查记录

### 1. `core/loop.py`

#### 文件概览
- 总行数：922（>300，但已通过 `_run_think_phase` / `_run_tool_phase` 拆分，主循环本体可读）
- 函数数量：6 个模块级函数 + `agent_loop` 内 7 个回调闭包
- 发现问题：🔴 1 / 🟡 4 / 🟢 2
- 整体评价：需重构（错误处理路径）
- 关键问题：模型失败的提前返回跳过终态事件发布，与 API 层契约断裂（见跨文件问题 C1）

#### 逐行批注

| 行号 | 级别 | 类别 | 原始代码 | 问题描述 | 建议修改 | 状态 |
|------|------|------|---------|---------|---------|------|
| 855-856 | 🔴 | 逻辑 | `if think_outcome.return_from_loop: return current_state` | 模型错误时提前 return，跳过 `record_turn()`、`_publish("loop.completed")`（891 行）、agent span 属性设置（911-920）。下游 sse_adapter 只认 `loop.completed` 作为终态事件 → 前端收不到错误终态 | 提前返回前补发 `loop.completed`（status="error"）与 span 属性；或抽出 `_finalize_run()` 统一收尾 | 待修复 |
| 329-332 + 906-909 | 🟡 | 逻辑 | 两处 `record_agent_request(...)` | loop 内对所有返回路径都记了指标；调用方 `agents/base.py:383-386` 对每次正常返回又记一次 → 每次 agent 运行被计 2 次，Prometheus 计数翻倍 | 指标只保留一处（建议留 loop 内；`base.py` 仅保留 379-381 except 分支的记录） | 待修复 |
| 186-188 | 🟡 | 逻辑 | `transition_async(current_state, "blocked", reason)` | input 层 guard 在转为 "thinking" 之前执行，此时状态为 `idle`（首轮）或 `observing`（后续轮）；`VALID_TRANSITIONS` 中两者均不允许 →blocked，非严格模式下每次拦截都打 warning + 记 invalid 指标 | `state_machine.py:46-54` 给 `idle`/`observing` 的允许集补 `"blocked"` | 待修复 |
| 590-595 | 🟡 | 结构 | `transition_async(current_state, current_state.status, ...)` | hints 已用 `model_copy` 直接改了 status（loop_hints.py:250-255），此处自转换被 `state_machine.py:82-84` 判为 no-op：无 transition_id、不发布 state.transition 事件，审计/回放缺一环 | hints 不直接改 status，返回终止原因，由 loop 经状态机完成转换 | 待重构 |
| 308 | 🟡 | 逻辑 | `if think.llm_response.usage:` | `record_llm_call`（调用次数）被 usage 是否存在门控；不返回 usage 的提供方会漏记调用次数 | `record_llm_call` 移出条件，仅 token 计数受 usage 门控 | 待修复 |
| 487-490 | 🟢 | 死代码 | `else: ... await _publish("state.transition", ...)` | `on_step` 形参恒为 `agent_loop` 传入的 `_on_step` 包装器（844/870 行），永不 falsy → else 分支不可达 | 删除 else 分支，或让 `_publish` 无条件执行 | 可选 |
| 731-748 | 🟢 | 结构 | `detail.split(",", 1)` | usage 以 `"prompt,completion"` 字符串经 `on_step` 传递再解析，是事件总线迁移期的临时协议（sse_adapter.py:195 还要再拼一次） | 迁移完成后改走结构化 `llm.usage` 事件，删除字符串协议 | 可选 |

#### 函数级审查

**函数名：`agent_loop`**
- 行数范围：662-922
- 职责描述：Agent 主循环 think→act→observe，组装回调、状态机、护栏、指标与事件发布

审查项：
- □ 输入校验：依赖 `AgentState` 的 pydantic 校验，OK
- □ 边界条件：`while not current_state.is_terminal()` 无独立兜底的最大墙钟/最大轮次外的退出依赖 state 内的 max_steps，OK
- □ 异常处理：**无全局兜底**——回调（`on_tool_result`、`event_bus.publish` 经 `_publish`）抛错会穿透循环，audit 不 finalize、终态事件不发布（`agents/base.py:379-381` 只记指标再抛出）。建议回调调用点隔离 try/except
- □ 副作用：`guardrail_system.on_event = _on_guardrail_event`（815 行）直接改写（可能为调用方共享的）护栏系统对象；当前无外部传入方，属潜在隐患
- □ 返回值：始终返回 `AgentState`，契约一致

发现问题：
- 856 🔴 逻辑：模型错误提前返回跳过终态事件（见批注表）
- 906 🟡 逻辑：与 base.py 重复记录 `record_agent_request`

**函数名：`_run_tool_phase`**
- 行数范围：370-659
- 职责描述：单轮工具阶段——output/tool/post_tool 三层护栏、权限门、执行、观察、hints、审计、微压缩

审查项：
- □ 输入校验：OK（`_parse_error` 参数在 loop_phases.py:232 被拦截为工具错误结果反馈给模型，设计良好）
- □ 边界条件：权限拒绝采用"全有或全无"（任一工具被拒则整轮 blocked），属设计选择，建议文档化
- □ 异常处理：工具执行异常在 `execute_tools_phase` 内被转为 `ExecutionResult.from_error`（好）；但 `on_tool_result` 回调在 try 外（loop_phases.py:280-282）
- □ 副作用：`consecutive_exploratory` 计数从 `guard_result.metadata` 镜像（571-573 行），与 ExploreLoopGuard 内部状态形成隐式耦合，键名变更无编译期保护
- □ 返回值：`_ToolPhaseOutcome` dataclass，一致

---

### 2. `core/model.py`

#### 文件概览
- 总行数：704（>300，建议将 `_parse_xml_tool_calls` 等 vLLM 兼容逻辑拆到独立适配模块）
- 函数数量：14（4 个 ModelClient 实现 + 解析辅助函数）
- 发现问题：🔴 0 / 🟡 1 / 🟢 3
- 整体评价：清晰
- 关键问题：`_repair_json` 启发式修复行为不可预测且缺测试锁定

#### 逐行批注

| 行号 | 级别 | 类别 | 原始代码 | 问题描述 | 建议修改 | 状态 |
|------|------|------|---------|---------|---------|------|
| 122-136 | 🟡 | 逻辑 | `_repair_json` | ①`(?<!\\)'` → `"` 会把双引号字符串值内的撇号一并替换（注释自称 "careful with apostrophes"，但正则并不区分上下文）；②`([{,]\s*)(\w+)(\s*:)` 可匹配字符串值内部的 `, xxx:` 片段。多数情况回落 `_parse_error`，但存在"修复成合法但内容被改"的残余风险，且无单元测试锁定行为 | 仅在明显单引号风格时启用；或换 json5 类容错解析；至少补测试锁定当前行为 | 待修复 |
| 75-90 | 🟢 | 死代码 | `def _max_nesting_depth` | `ast.literal_eval` 兜底已删（50-52 行注释），此预检函数成为死代码，全仓无调用（含测试） | 删除 | 可选 |
| 600-616 | 🟢 | 契约 | `reasoning_content=None`（614 行） | `BackendModelClient.generate_stream_full` 只转发 reasoning token 不累积，最终响应 `reasoning_content` 恒为 None；`OpenAIModelClient` 会累积返回。同接口两实现行为不一致，影响 think_phase 的 reasoning-loop 检测对 backend 路径失效 | 累积 reasoning 并填入返回值 | 可选 |
| 182 | 🟢 | 逻辑 | `id=f"call_{len(tool_calls)}"` | XML 工具调用的合成 id 跨轮重复（call_0/call_1）；参数值全为字符串无类型转换 | id 加 uuid 后缀；类型转换交给下游 schema 校验即可 | 可选 |

#### 函数级审查

**函数名：`_parse_tool_arguments`**
- 行数范围：24-72
- 职责描述：容错解析 LLM 输出的工具调用参数

审查项：
- □ 输入校验：非 dict 结果拒绝并标记 `_parse_error`，好
- □ 边界条件：三级 fallback（标准 JSON → ref 引号修复 → JSON 修复）链条清晰
- □ 异常处理：仅捕获 `JSONDecodeError`，精确，好
- □ 副作用：无
- □ 返回值：失败返回 `{"_parse_error": True, "raw": raw}` 哨兵，下游 loop_phases.py:232 有对应处理，契约闭环

**函数名：`OpenAIModelClient.generate_stream_full`**
- 行数范围：396-502
- 职责描述：流式生成，分离 reasoning/content 回调，累积工具调用增量

审查项：
- □ 输入校验：OK
- □ 边界条件：finish_reason="tool_calls" 但无工具数据时回落非流式（489-500 行），对 vLLM 怪异行为防御到位
- □ 异常处理：回落失败时记录并重新抛出，由 think_phase 捕获，链条完整
- □ 副作用：`on_token`/`on_content_token` 回调抛错会中断流式消费（与 loop 层回调穿透问题同源）
- □ 返回值：`_normalize_response` 统一出口，一致

---

### 3. `core/state.py`

#### 文件概览
- 总行数：358（略超 300，内聚度高，可接受）
- 函数数量：12
- 发现问题：🔴 1 / 🟡 0 / 🟢 1
- 整体评价：清晰
- 关键问题：max_steps 截断在消息历史中留下"带 tool_calls 但永远无结果"的 assistant 消息

#### 逐行批注

| 行号 | 级别 | 类别 | 原始代码 | 问题描述 | 建议修改 | 状态 |
|------|------|------|---------|---------|---------|------|
| 141-162 | 🔴 | 逻辑 | max_steps 分支 | 达到 max_steps 时：assistant 消息仍带 `tool_calls` 追加进 messages（144-150），但 pending 被清空（158）、状态置 completed → 这批 tool_calls 永远不会有对应 tool 结果消息。该历史经 `stream_service.py:382` 落库后，下一轮 resume 时 `to_openai_messages()` 产出 assistant(tool_calls) 后无 tool 消息，OpenAI 兼容 API 直接 400 → **触发过 max_steps 的会话无法多轮继续** | max_steps 截断时追加的 assistant 消息剥离 tool_calls（或补 tool 结果占位消息） | 待修复 |
| 310-332 | 🟢 | 语义 | `record_turn` | `_run_tool_phase`（loop.py:510）每轮记录 + `agent_loop` 结束（loop.py:889）再记录；工具阶段后终止的路径会在 conversation tree 中产生同轮双节点（后者含 hints/compact 后的消息），节点语义含糊 | 明确"每轮一个节点"或"每运行一个终态节点"，去掉重复调用 | 可选 |

#### 函数级审查

**函数名：`add_observation`**
- 行数范围：184-280
- 职责描述：把工具结果追加为 tool 消息，inline skill 指令批量转为 user 消息

审查项：
- □ 输入校验：结果数与 tool_calls 数不匹配时抛 `ValueError`，严格，好
- □ 边界条件：inline 指令在所有 tool 消息之后批量注入，符合 OpenAI 消息序列约束，注释清晰
- □ 异常处理：`json.dumps` 失败有 `_json_default` 回退 + 双层 except，稳健
- □ 副作用：无（不可变 copy）
- □ 返回值：新 AgentState，一致

**函数名：`add_thought`**
- 行数范围：120-182
- 职责描述：把模型响应（工具调用或文本）追加进历史并驱动步进/终止

发现问题：
- 156-159 🔴 逻辑：max_steps 截断残留无结果的 tool_calls 消息（见批注表）

---

### 4. `core/event_bus.py` + `core/events.py`

#### 文件概览
- 总行数：148 + 55
- 函数数量：8 + 1
- 发现问题：🔴 0 / 🟡 0 / 🟢 2
- 整体评价：清晰（本次审查中最干净的模块）
- 关键问题：无实质问题；`block` 背压策略有操作风险

#### 逐行批注

| 行号 | 级别 | 类别 | 原始代码 | 问题描述 | 建议修改 | 状态 |
|------|------|------|---------|---------|---------|------|
| event_bus.py:132-133 | 🟢 | 运维 | `await queue.put(event)` | `block` 策略下任一停滞订阅者会阻塞整个 agent 循环；默认 `drop_oldest` 无此问题 | 部署文档注明 `block` 的风险，或加订阅者滞后指标 | 可选 |
| events.py:44 | 🟢 | 文档 | `payload: Event-specific data. Each EventType documents its schema.` | 实际并无任何 per-type schema 文档，docstring 过度承诺 | 补 schema 表或改注释 | 可选 |

#### 函数级审查

**函数名：`EventBus.publish`**
- 行数范围：106-141
- 职责描述：向全部匹配订阅者非阻塞投递，按策略处理背压

审查项：
- □ 输入校验：未知策略有安全回落（drop newest + warning），好
- □ 边界条件：队列满时 drop_oldest 先取后放，asyncio 单线程下无竞态
- □ 异常处理：`QueueFull`/`QueueEmpty` 处理完整；订阅列表快照遍历防并发修改，好
- □ 副作用：丢弃事件有 `record_event_bus_dropped` 指标，可观测性好
- □ 返回值：无

**正向评价**：背压三策略完备、丢弃有指标、订阅有过滤双保险（发布时 + 消费时），是本模块应有的样子。

---

### 5. 交叉依赖文件（`loop_phases.py` / `loop_hints.py` / `loop_guards.py` / `state_machine.py`）

#### 逐行批注

| 文件:行号 | 级别 | 类别 | 问题描述 | 建议修改 | 状态 |
|-----------|------|------|---------|---------|------|
| `loop_phases.py:91` | 🟡 | 契约 | `getattr(model, "_temperature", None)` 访问私有属性；`ModelClient` 协议及全部实现均有公开 `temperature` property（model.py:305）。自定义 client 会在审计日志中留下 temperature=None | 改为 `getattr(model, "temperature", None)` | 待修复 |
| `loop_phases.py:168-172` | 🟡 | 健壮性 | `response.usage['prompt_tokens']` 直取键：提供方漏键时 KeyError 在 try 块外抛出 → 循环崩溃；且 usage 走 `"prompt,completion"` 字符串协议（与 loop.py:731 批注同源） | `usage.get(..., 0)`；迁移到结构化事件 | 待修复 |
| `loop_phases.py:228` | 🟢 | 性能 | 工具串行执行（for 循环逐个 await）；一轮多个独立工具调用无法并发 | 评估 registry 并发安全后用 `asyncio.gather` | 可选 |
| `loop_hints.py:250-255` | 🟡 | 结构 | 用 `model_copy` 直写 `status="completed"`，绕过状态机（与 loop.py:801 注释"all status transitions go through this object"冲突），无 transition_id、无 state.transition 事件 | 返回终止原因，由 loop 经 `state_machine` 转换 | 待重构 |
| `loop_hints.py:212-228` | 🟡 | 逻辑 | `consecutive_exploratory == 0` 即注入"就绪提示"；该计数在任何非探索性工具调用后都为 0 → 正常流程中**每轮工具阶段都可能重复追加同一条 [系统提示] user 消息**，且无 `_inject_reminder` 那样的去重 → 历史线性膨胀、浪费 token | 注入前按内容去重（可复用 reminder 的 `source` 标记模式） | 待修复 |
| `loop_guards.py:18-166` | 🟢 | 死代码 | `check_explore_loop`、`check_business_artifact_progress`、`update_null_tracking`、`update_tool_call_history`、`update_exploratory_tracking` 共 ~120 行已无调用方（含测试），功能由 `guardrails/loop_guardrails.py` 迁移版接管；仅 `detect_reasoning_loop` 仍在用 | 删除死函数，保留 `detect_reasoning_loop` | 可选 |
| `state_machine.py:46-54` | 🟡 | 逻辑 | 转移表中 `idle`/`observing` 不允许 →`blocked`，但 loop.py:186 input 层拦截恰从这两个状态发起 → 每次拦截都触发 invalid 警告日志 + 指标 | 转移表补 `blocked` | 待修复 |

---

## 问题汇总

### 🔴 阻塞项（立即修复）

1. **跨文件：模型失败被静默为"正常完成"**
   - 链路：`loop.py:855-856` 模型错误提前 return → 跳过 `loop.completed` 发布（loop.py:891）；`sse_adapter.py:181-191` 不转发终态 `state.transition`（error/blocked/completed 无映射）；`stream_service.py:393,447-458` runner 不检查 `final_state.status`，一律走 complete 分支，会话落库为 `completed`
   - 影响：LLM 超时/限流/鉴权失败时，用户看到空结论的"完成"，无任何错误提示
   - 修复：①`agent_loop` 提前返回前补发 `loop.completed`（status="error"）；②sse_adapter 转发终态 transition；③runner 对 `final_state.status == "error"` 走 error 分支
   - 验证：mock 模型抛异常跑 `tests/agent/test_loop.py` 断言终态事件；端到端断网发起会话，前端应见错误而非空白完成

2. **`state.py:141-162`：max_steps 截断残留无结果 tool_calls 消息**
   - 影响：触发 max_steps 的会话落库后无法 resume，下一轮 API 调用 400
   - 修复：截断时追加的 assistant 消息剥离 tool_calls
   - 验证：构造 `max_steps=1` + 模型返回 tool_calls 的单测，resume 后 `to_openai_messages()` 应通过 OpenAI 消息序列校验

### 🟡 警告项（本周内修复）

1. `loop.py:329/906` + `agents/base.py:383`：`record_agent_request` 双重计数，请求量指标翻倍 → 指标只留一处
2. `state_machine.py:46-54`：`idle`/`observing` → `blocked` 为非法转换，input 层护栏每次拦截都产生 invalid 告警噪音 → 转移表补 `blocked`
3. `loop_hints.py:250-255` + `loop.py:590-595`：hints 强制终止绕过状态机（无 transition_id / 无事件），自转换兜底为 no-op → 改由 loop 经状态机转换
4. `loop_hints.py:212-228`：终端工具就绪提示在 `consecutive_exploratory==0` 时每轮重复注入，无去重 → 注入前按内容去重
5. `loop.py:308`：`record_llm_call` 被 usage 是否存在门控，不返回 usage 的提供方漏记调用次数 → 移出条件
6. `loop_phases.py:91`：访问模型私有属性 `_temperature`，违反协议抽象 → 改公开 `temperature` property
7. `loop_phases.py:168-172` + `loop.py:731-748`：usage 字符串协议 + 直取键 KeyError 风险 → `usage.get()` + 结构化事件
8. `model.py:122-136`：`_repair_json` 启发式修复无测试锁定、可能改坏字符串值 → 限制启用条件 + 补测试
9. `agent_loop` 无全局异常兜底：回调抛错穿透循环，audit 不 finalize → 回调调用点隔离 try/except

### 🟢 建议项（下次迭代处理）

1. `loop_guards.py:18-166`：~120 行死代码（迁移遗留），仅 `detect_reasoning_loop` 在用 → 删除
2. `model.py:75-90`：`_max_nesting_depth` 死代码（ast.literal_eval 时代遗留）→ 删除
3. `loop.py:487-490`：else 分支不可达（on_step 恒为包装器）→ 删除
4. `model.py:600-616`：`BackendModelClient` 流式不累积 reasoning_content，与 OpenAI 实现契约不一致 → 补齐
5. `model.py:182`：XML 工具调用合成 id 跨轮重复 → 加唯一后缀
6. `loop_phases.py:228`：工具串行执行，可评估并发
7. `event_bus.py:132`：`block` 背压策略下停滞订阅者会拖住整个循环 → 文档化风险
8. `events.py:44`：docstring 声称有 per-type schema 文档，实际没有 → 补文档或改注释
9. `state.py:310-332`：`record_turn` 双调用产生同轮双树节点 → 明确节点语义
10. `agents/base.py:383-386`：`status if status != "error" else "error"` 两分支同值的死条件 → 随指标双计修复一并清理

---

## 技术债务记录

- `core/loop.py` ↔ `core/loop_phases.py` ↔ `api/sse_adapter.py`：legacy 回调与事件总线双轨并行（usage 字符串协议、`on_step` 事件名约定），事件总线迁移完成后应删除回调链
- `core/loop_guards.py`：迁移遗留死代码待删（见 🟢-1）
- `loop_hints.py`：hints 直接改 state 的模式与状态机设计目标冲突，待统一（见 🟡-3）
- 前端对 `loop_completed` SSE 事件的依赖程度本次未审（属 webui 范围），修复 🔴-1 时需同步确认前端终态处理
- `audit_logger.py` / `loop_guards.py` 各有一条 E501 超限行 + I001 导入排序，lint 门当前不绿

---

## 审查者自检

- [x] 每个 🔴 都有明确的修复方案和验证方式
- [x] 标记了需要拆分的超大文件（`loop.py` 922 行、`model.py` 704 行，均有初步拆分但可继续）
- [x] 发现了跨文件的重复逻辑（指标双计：loop.py ↔ base.py；usage 协议三处接力：loop_phases ↔ loop ↔ sse_adapter）
- [x] 记录了"这次没审但下次要注意"的技术债务（webui 终态处理、context_manager、guardrails 实现细节）
- [x] 已确认修复后的回归测试范围（`tests/agent/test_loop.py`、`test_model.py`、`test_state.py`，🔴 两项需新增用例）

---

## 附录：本次审查发现的典型模式

- **模式 1：绕过状态机直写 status**。`loop_hints.py:250`、`state.errored(set_status=False)` 等多处用 `model_copy` 直接改状态字段，与"所有转换经状态机"的设计目标冲突，导致审计事件缺失。教训：状态字段写入应有唯一入口。
- **模式 2：跨层重复记账**。同一指标在 loop 层与 agent 层各记一次；同一 usage 数据以字符串协议在三层间接力解析。教训：指标/事件应单一生产者。
- **模式 3：迁移期双轨遗留**。legacy 回调 + 事件总线并行、迁移后旧 guard 函数成死代码。教训：迁移计划（pi-architecture-migration-plan）每阶段应带"旧路径删除"检查项。
- **模式 4：LLM 输出容错启发式无测试锁定**。`_repair_json`、`_fix_unescaped_ref_quotes` 行为随输入不可预测。教训：容错代码必须有行为锁定测试，且优先回落到显式错误而非"猜"。
- **模式 5：终态路径的事件缺失**。正常路径事件完备，错误/强制终止路径（模型错误、hints 强制终止）各缺一环。教训：事件发布应集中在统一 finalize 函数，而非散在各 return 点前。


---

# 修复与回归验证记录（2026-07-17 同日）

**修复人**：Kimi Code（AI agent）　**验证环境**：`courtier/.venv`（Python 3.12）

## 回归结果

- **核心测试**：`tests/agent/test_loop.py + test_model.py + test_state.py` → **58 passed**（较审查时 +12 项新用例）
- **全量非集成测试**：`uv run python -m pytest -m "not integration"` → **937 passed, 6 skipped**；18 errors 全部位于 `tests/agent/api/test_routes.py`，原因是本环境缺少有效 `MYSQL_URL`（`sqlalchemy.exc.ArgumentError: Could not parse SQLAlchemy URL`），为既有环境问题，与本次改动无关
- **ruff**：本次改动涉及的全部文件 `All checks passed`（仓库其余范围仍有 50 个既有 lint 错误，未在本次范围内处理，见遗留债务）
- **mypy**：仓库存在 13 个既有错误（含缺失 yaml stubs），非通过门禁；本次改动未新增错误类别
- 说明：本环境 `uv run pytest` 入口脚本指向外部 venv，需用 `uv run python -m pytest` 运行；`uv sync` 后 slowapi 缺失问题消失

## 逐项状态

### 🔴 阻塞项

| # | 问题 | 状态 | 修复方式与验证 |
|---|------|------|---------------|
| 1 | 模型失败被静默为"正常完成" | ✅ 已修复 | ① `loop.py` 模型错误路径不再提前 return，改为 break 后经**共享尾部**统一收尾（loop.completed 事件、audit finalize、指标、span 属性）；② `stream_service.py` runner 检查 `final_state.status == "error"` 改走 error 分支（前端收到 `{"type": "error"}`，会话落库 `status="error"`）；报告中"sse_adapter 转发终态 transition"经评估为冗余（终态由补发的 loop.completed + runner error 覆盖），未采纳。验证：`test_model_error_publishes_loop_completed`、`test_unexpected_phase_error_is_contained` |
| 2 | max_steps 截断残留无结果 tool_calls | ✅ 已修复 | ① `state.py:add_thought` 截断时追加的 assistant 消息剥离 tool_calls；② `to_openai_messages()` 序列化时净化任何无对应 tool 结果的 assistant tool_calls（防御纵深，覆盖 blocked 等同类路径）。验证：`test_max_steps_strips_tool_calls_from_assistant_message`、`test_to_openai_messages_drops_unanswered_tool_calls`、`test_to_openai_messages_keeps_answered_tool_calls` |

### 🟡 警告项

| # | 问题 | 状态 | 修复方式 |
|---|------|------|---------|
| 1 | `record_agent_request` 双重计数 | ✅ | 指标统一在 loop 尾部记录；删除错误分支提前记录与 `agents/base.py` 的重复记录（base 仅保留 except 兜底与 latency） |
| 2 | `idle/observing → blocked` 非法转换 | ✅ | `state_machine.py` 转移表：`idle`/`observing` 补 `blocked`，`idle` 补 `error`（全局兜底路径） |
| 3 | hints 强制终止绕过状态机 | ✅ | `check_and_inject_hints` 签名改为返回 `(state, force_complete_reason)`，终止转换由 loop 经 `AgentStateMachine` 完成（有 transition_id + state.transition 事件） |
| 4 | 就绪提示每轮重复注入 | ✅ | 三处注入统一走 `_append_hint_message`：`source="hint"` 标记 + 按内容去重。验证：`test_append_hint_message_dedupes_identical_hints` |
| 5 | `record_llm_call` 被 usage 门控 | ✅ | 调用计数移出条件，仅 token 计数依赖 usage |
| 6 | 访问私有属性 `_temperature` | ✅ | 改用公开 `temperature` property |
| 7 | usage 字符串协议 + KeyError | ✅（部分） | `usage.get(..., 0)` 消除 KeyError；字符串协议本身保留至事件总线迁移完成（计入技术债务） |
| 8 | `_repair_json` 启发式脆弱 | ✅ | 单引号替换仅在纯单引号风格（全文无双引号）时启用；新增 5 项行为锁定测试（混合引号/撇号/尾逗号/裸键/转义引号） |
| 9 | agent_loop 无全局异常兜底 | ✅ | legacy 回调经 `_safe_call` 隔离；`_publish` 失败仅记日志；主循环全局 except 将状态机转 error 后走共享尾部（CancelledError 不受影响） |

### 🟢 建议项

| # | 问题 | 状态 | 说明 |
|---|------|------|------|
| 1 | `loop_guards.py` ~120 行死代码 | ✅ 已删除 | 仅保留 `detect_reasoning_loop` |
| 2 | `model.py:_max_nesting_depth` 死代码 | ✅ 已删除 | — |
| 3 | act else 死分支 | ✅ 已修复 | 改为无条件发布 act 转换事件——顺带修复了纯事件总线消费者（SSE）此前收不到 act 事件的隐藏缺陷 |
| 4 | BackendModelClient 不累积 reasoning | ✅ 已修复 | 流式累积并返回 `reasoning_content` |
| 5 | XML 工具调用 id 跨轮重复 | ✅ 已修复 | id 加 uuid 短后缀 |
| 6 | 工具串行执行 | ⏸️ 不改（评估结论） | 插件经 stdio JSON-RPC 单通道通信，并发执行会在 stdin 交错，不安全；维持串行 |
| 7 | `block` 背压风险 | ✅ 已文档化 | EventBus docstring 注明风险并推荐 `drop_oldest` |
| 8 | events.py docstring 过度承诺 | ✅ 已修正 | — |
| 9 | `record_turn` 双调用 | ✅ 已修复 | `turn_pending_record` 跟踪：工具阶段观察后记录一次，或循环尾部（think 后终止）记录一次；被拦截轮次不进入会话树 |
| 10 | base.py 死条件表达式 | ✅ 已删除 | 随 🟡-1 一并清理 |

## 改动文件清单

- `courtier/agent/core/loop.py`（🔴-1、🟡-1/5/9、🟢-3/9）
- `courtier/agent/core/state.py`（🔴-2、source 标记）
- `courtier/agent/core/state_machine.py`（🟡-2）
- `courtier/agent/core/loop_hints.py`（🟡-3/4，重写）
- `courtier/agent/core/loop_phases.py`（🟡-6/7）
- `courtier/agent/core/loop_guards.py`（🟢-1，重写为仅保留推理循环检测）
- `courtier/agent/core/model.py`（🟡-8、🟢-2/4/5）
- `courtier/agent/core/event_bus.py`、`events.py`（🟢-7/8 文档）
- `courtier/agent/core/audit_logger.py`（E501 修复）
- `courtier/agent/agents/base.py`（🟡-1/🟢-10）
- `courtier/agent/api/services/stream_service.py`（🔴-1③）
- `tests/agent/test_loop.py`、`test_state.py`、`test_model.py`（+12 项用例）

## 遗留技术债务（更新）

- 仓库其余范围 50 个既有 ruff 错误（`api/routes/`、`tests/` 等），建议单独安排 lint 清理批次
- `tests/agent/api/test_routes.py` 需有效 `MYSQL_URL` 才能在本环境运行（18 个 error）
- usage 字符串协议（`loop_phases.py` ↔ `loop.py` ↔ `sse_adapter.py`）待事件总线迁移完成后删除
- 工具并发执行维持串行结论（插件 stdio 单通道约束），如未来插件协议支持并发再评估
- mypy 全仓 13 个既有错误（含缺失 `types-PyYAML` stubs），如需纳入门禁需单独治理


---

# 遗留债务清理记录（2026-07-17 第二批）

在第一批修复（审查发现项）之后，对回归记录中"遗留技术债务"节所列事项执行了清理。执行方式：主代理处理核心文件与跨域问题，6 个并行子代理分两波处理 ruff 手工修复与 mypy 治理。

## 清理结果总览

| 债务项 | 状态 | 结果 |
|--------|------|------|
| 全仓 ruff 既有错误 | ✅ 清零 | 301 → 0（172 自动修复 + 129 手工修复，覆盖 courtier/、tests/、libs/、domains/、plugins/、alembic/、scripts/） |
| `test_routes.py` 依赖 MYSQL_URL | ✅ 根治 | `app.py` lifespan 在空 `mysql_url` 时跳过引擎创建（与 `bootstrap_admin_user` 既有守卫一致），恢复设计的 no-DB 模式；18 项测试全部复活且无需数据库 |
| usage 字符串协议 | ✅ 收敛+评估 | SSE 侧内部"拼串再解析"往返已移除（`_apply_usage` 结构化路径）；think_phase → `on_step("usage")` 的 legacy 输入契约保留（`agents/base.py` 公开 API、测试锁定），完整移除清单见下 |
| mypy 全仓既有错误 | ✅ 清零 | 161 个源文件 `Success: no issues found`（原 78 个）；dev 依赖新增 `types-PyYAML` |
| 工具并发执行 | ⏸️ 维持结论 | 插件 stdio JSON-RPC 单通道约束，并发不安全，不改 |

**最终回归**：`ruff check .` 全绿；`mypy courtier/` 161 文件零错误；`pytest -m "not integration"` **955 passed, 6 skipped, 0 errors**。

## mypy 清理中发现的 4 个真实运行时 bug（已全部修复）

1. **`context_manager.py:311` — 冻结 dataclass 被赋值**：`msg.content = ...` 作用于 frozen `Message`，压缩失败的回退路径必抛 `FrozenInstanceError`。改为 `dataclasses.replace()`。
2. **`plugin/sdk/runtime.py` — `connect_read_pipe` 二元组未解包**：返回的 `(transport, protocol)` 整体被当作 transport 保存，导致关停时 `transport.close()` 静默失败（异常被吞）。已解包并收窄类型为 `ReadTransport`。
3. **`routes/profile.py` — 用户改密码静默失效（严重）**：`update_data["password"]` 被 `ProfileUpdate`（pydantic extra=ignore）丢弃，改密码变成无声空操作；且键名与 ORM 列 `password_hash` 不符。改为 `password_hash` 键 + dict 直传 `user_repo.update()`。
4. **`session_store.py:338` — except 处理器内必抛 `FrozenInstanceError`**：`SessionRecord` 是 frozen dataclass，`session._dirty = True` 不可能成立；且 `_dirty` 无任何读取方。删除该赋值，持久化失败回到"仅记日志"的设计意图。

另按同构模式修复子代理上报的范围外同类 bug：**`routes/admin_users.py` 管理员重置密码**（`UserUpdate.password` → ORM `password_hash` 列名不匹配，运行时必抛 `CompileError`），同样改为 `password_hash` 键 + dict 直传。

> 注：profile/admin 改密路径在无数据库环境下无自动化测试覆盖，修复经代码审查与直接实验验证；如需回归测试，需配置测试数据库后补充。

## usage 字符串协议未来移除清单（事件总线迁移完成时执行）

1. 删除 `loop_phases.think_phase` 的 `on_step("usage", ...)` 字符串发射；`llm.usage` 事件改由 `_run_think_phase` 从 `think.llm_response.usage` 直接结构化发布
2. 删除 `loop._on_step` 的 usage 解析分支
3. 删除 `sse_adapter.on_step("usage")` / `_handle_usage` 字符串解析入口（`_apply_usage` 保留）
4. 同步更新 `tests/agent/api/test_sse_adapter.py:427`（字符串输入用例）与 `agents/base.py` 的 `on_step` 文档

## 改动规模

- ruff 手工修复：42 个文件（三批子代理：非 agent 源码 15 个、agent 包 8 个、tests/libs/plugins 19 个）
- mypy 治理：25 个文件（含主代理修复的 core/config/db 边界 9 个文件）
- 主代理直接修复：`api/app.py`（lifespan no-DB 守卫）、`api/sse_adapter.py`（usage 结构化）、`api/routes/admin_users.py`（密码列名）、`core/loop.py`、`core/loop_phases.py`、`core/cache_store.py`、`core/context_manager.py`、`artifacts/store.py`、`artifacts/resolver.py`、`tools/registry.py`、`config.py`
- dev 依赖：`+ types-PyYAML`

## 剩余已知事项（非债务，为环境/设计说明）

- 本环境需用 `uv run python -m pytest` / `uv run python -m mypy`（`uv run pytest` 入口脚本指向外部 venv `.openharness-venv`，缺少项目依赖）
- 需真实数据库的集成测试（`-m integration`）未在本环境运行
