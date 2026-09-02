# Refusal 检测与同模型重试 — 实施方案

## 背景与目标

模型偶发拒绝执行任务（安全策略误伤、能力边界表述），当前循环把拒绝文本直接当结论交给用户，无任何提示与恢复。目标：对无工具调用的纯文本响应做 refusal 检测，命中后**同模型重试 N 次**；重试耗尽则明确告知用户"当前模型多次拒绝、可能不可用"，轮次正常收尾。**不做跨模型降级**（对齐结论，用户明示）。检测器定位为**模型行为恢复策略**，不进 guardrails 管线（护栏管策略执行，本计划管失败恢复）。

## 对齐结论（已与用户确认，2026-09-02）

1. **策略链**：同模型重试 N 次 → 告知用户当前模型不可用 → 正常收尾；**不进行池内降级换模型**。
2. **检测机制**：可配置中英模式清单（settings JSON，内置最小集）；LLM judge 不做。
3. 检测范围：仅无 `tool_calls` 的纯文本响应（发起工具调用的响应不参与检测）。
4. 定位：think 阶段策略组件 + output scope observer 形态的检测遥测可选；不改 guardrails 词汇。

## 侦察结论（已验证事实）

- **模型池已落地**（本计划不依赖它，但"不降级"决定使其无交集）：`resolve_model_profile` / 每档案客户端（`49a7ba7`）、每运行选择（`0eaf02c`）、`run_model_profile` 通道（`run_context.py`）。
- **循环无模型调用重试先例**（loop.py 无 retry/attempt 逻辑）——本计划是第一处，需定义事件口径。
- guardrails 计划 T0 将删除玩具 `RefusalOutputGuard`（英文词表、log-only）——本计划的 `RefusalDetector` 是其真正替代，无代码继承。
- 错误口径：模型可见/用户可见文案统一走 `errors.*` 模板（error unification 惯例，locale 渲染）。
- SSE：think/token 事件为流式渲染单位；`usage` 事件按轮聚合——重试消耗天然计入预算。

## 总体设计

### 1. 检测器

- `RefusalDetector`：模式清单来自 settings `refusal_patterns`（JSON 字符串数组，默认内置中英最小集，如「我无法 / 我不能 / 无法协助 / I cannot / I'm sorry, but」——实现时按实测校准）；`refusal_detection_enabled` 总开关（默认 on，hot 类）。
- 检测点：think 阶段模型响应之后；仅当响应无 `tool_calls` 且文本非空。
- 命中即发 `refusal_detected` 事件（SSE + 遥测计数器），载荷带命中模式片段（截断）。

### 2. 重试策略

- 命中后在 think 阶段内重试同一模型：重新发起模型调用（不改温度、不改消息——原样重问；拒绝常是采样波动）。`refusal_retry_max` 默认 1（总尝试 = 1 + N）。
- 事件口径：每次重试发 `think_retry` 事件（载荷：`attempt` 序号），前端收到后**重置当前步骤的流式缓冲**重新渲染——避免同屏拼接多次尝试的文本。
- 消耗计入本 turn 预算（usage 聚合天然覆盖）；重试不受步数上限影响（仍在同一 step 内），但受整体 token 预算约束——预算耗尽按既有预算路径收尾。

### 3. 耗尽出口

- 最后一次响应照常进入对话流（不隐藏模型输出）；轮次**正常完成**（不转 error 终态——拒绝不是系统故障）。
- 前端横幅提示：新 SSE 事件 `refusal_exhausted` + `errors.*` 新模板键（中英双语，口径："模型多次拒绝执行该任务，当前模型可能不可用；请稍后重试或联系管理员更换模型"）。
- 遥测：`refusal_exhausted` 计数（按 model 维度），供 admin 判断换池条目。

### 4. 配置面

settings 三键（均 hot、`refusal_` 前缀自动进 admin 设置页）：`refusal_detection_enabled`（bool）、`refusal_patterns`（JSON list）、`refusal_retry_max`（int ≥ 0；0 = 只检测告警不重试）。

## 任务拆分（每任务独立提交，conventional commits）

- **T1 检测器与配置**：`RefusalDetector` + 三键 settings + `refusal_detected` 事件/遥测。单测：命中/不命中/开关关闭/模式空。
- **T2 think 重试**：重试环 + `think_retry` 事件 + 预算计入。单测：第 K 次成功即止、耗尽出口、预算耗尽路径、带 tool_calls 响应不触发。
- **T3 耗尽出口**：`refusal_exhausted` 事件 + `errors.*` 模板键（中英）+ 遥测。单测：模板渲染 locale。
- **T4 webui**：`think_retry` 缓冲重置 + `refusal_exhausted` 横幅。node 测试：重置渲染、横幅展示。
- **T5 收尾**：全量测试 + 真机验收（注入临时模式命中真实会话 → 观察重试与横幅；未命中会话零行为差异）+ 文档（`AGENTS.md` §5.3 事件清单补两事件）+ 实施状态追加。

## 明确不做（本期）

- 跨模型降级/换模型（对齐结论 1；池内 fallback 链仍留待未来独立立项）。
- LLM judge 检测、温度/消息改写策略。
- 带工具调用响应的检测；按工具/按会话的检测开关。

## 验收标准

- 注入命中模式后：重试 N 次、每次 `think_retry` 可见、前端缓冲重渲染无拼接；耗尽后横幅文案双语正确、轮次正常完成。
- 未命中会话行为与改前逐字节一致（零开销路径：开关关/清单空/不命中）。
- `refusal_retry_max=0` 时只告警不重试。
- `uv run pytest` 全绿；webui `npm test`/`build` 通过；每任务独立提交。

## 实施状态（滚动更新）

- 2026-09-02：需求对齐完成（对齐结论 4 条），方案成文，待批准后排期（无跨计划依赖，可与 guardrails 并行）。
- 2026-09-02（批准后当日）：**T1-T5 全部落地**（cdecf14 → 725f62f）。要点与偏差：
  - T1：RefusalDetector（大小写不敏感子串匹配，返回原文片段）+ 三键 settings（guards 类、hot）+ `refusal_total{outcome}` 指标。
  - T2：think_phase 重试环——被拒响应**不入对话历史**（仅最终响应 add_thought）、usage 跨尝试累加进 `llm.usage`、`refusal.detected` + `think.retry` 事件（sse_adapter 显式分支 → SSE）。
  - T3：耗尽出口 `refusal.exhausted`（locale 文案经 `errors.refusal_exhausted` 模板随事件载荷下发），轮次正常完成。
  - T4：`think_retry` 处理器按 currentThoughtTurn 丢弃被拒尝试的流出文本并清空结论缓冲（历史 thought 不动）；`refusal_exhausted` 横幅挂 ChatArea 与输入区之间。
  - 验收：新增单测/事件测试全绿；与计划唯一细节偏差=think_phase 经显式 `publish` 参数接总线（方案未指明通道，on_step 字符串协议不带结构化载荷）。
