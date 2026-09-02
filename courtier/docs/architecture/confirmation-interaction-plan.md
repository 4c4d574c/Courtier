# 工具确认交互链路 — 实施方案

## 背景与目标

当前工具调用要么放行要么拒绝（permission gate / guardrails），没有"执行前需用户点头"的第三条路。目标：为配置进确认名单的工具建立**运行中挂起 → 用户裁决 → 恢复执行**的交互链路，覆盖 API、SSE/通知、webui 与持久化。本计划是 guardrails 统一计划（`guardrails-unification-plan.md`）中 `confirm` 词汇的消费方——**guardrails 计划先行**，本计划的运行时依赖其 T1-T3。

## 对齐结论（已与用户确认，2026-09-02）

1. **授权粒度**：会话内工具名级——每次弹窗三选：仅本次批准 / 本会话内该工具全部放行 / 拒绝；已批准集合随会话持久化。
2. **挂起策略**：无自动超时——run 保持 running 占 FIFO 位，用户可随时 `/api/stop` 终止。
3. **管线接入**：`CallGuardResult` 动作三值 `allow / deny / confirm`（guardrails T1 预留）；`ConfirmationGuard` 是 tool_call 层 guard，只裁决；挂起/恢复是循环侧动作（与"拒绝合成权在循环"同构）。
4. **策略来源**：v1 为 settings 配置名单 `tool_confirmation`（`[{tool, message}]`）；域声明确认名单走 guardrails T8 机制，后续再说。

## 侦察结论（已验证事实）

- **RunManager**：run 是后台 asyncio 任务，可 cancel（`run_manager.py:353-380` stop：执行中取消/排队中出队）；无暂停概念；重启 sweep 将 running 标记 interrupted（`run_manager.py:409-414`）——挂起中的 run 随进程消失，与现状口径一致。
- **SSE**：RunEventLog 是有界可重放 transcript，`confirmation_requested` 事件入库即天然支持断线重连重放；`GET /api/sessions/{id}/events?since=` 已有水印机制。
- **通知**：NotificationHub per-user run-status fan-out（全局 events 通道）是确认请求推送的现成通道。
- **持久化**：`SessionRecord` 支持字段扩展 + Alembic 迁移（既有惯例）；agent 按请求重建，会话级集合必须落库才能跨重建存活。
- **前置依赖**：guardrails 计划 T1（`CallGuardResult` 词汇）、T2（tool_call 派发）、T3（循环拒绝合成分支——confirm 分支与其同构）；旧 `require_confirmation` 死代码由 guardrails T5 删除，本计划是其真正的替代实现。

## 总体设计

### 1. 词汇与守卫

- `CallGuardResult.action` 扩为 `allow / deny / confirm`（guardrails T1 预留，本计划消费）。
- `ConfirmationGuard`（tool_call 层 guard）：构造注入确认名单 + 会话已批准集合的访问器；判定顺序——**会话已批准集合命中 → allow**（先于名单）；名单命中 → `confirm`（metadata 带 message）；否则 allow。
- 名单 settings 键 `tool_confirmation`：JSON `[{tool: str, message: str}]`，hot 类，admin 设置页可编辑。

### 2. 运行时挂起与恢复

- RunManager 增加 per-run pending 表 `{confirmation_id: asyncio.Future}`。
- 循环在工具分派点收到 `confirm` 决策：生成 `confirmation_id` → 注册 pending → 发 `confirmation_requested` 事件 + 通知 → `await future`（未来三种结算：approve → 继续执行该调用；deny → 合成 `confirmation_denied` 错误结果，路径复用拒绝合成；任务取消 → await 自然打断，run 走 stop/interrupted 既有口径）。
- 同批多个 `confirm`：**逐个串行询问**（v1 不做批量合并）。
- `approve_session`：结算 future 为 allow，同时把工具名写入会话已批准集合（见 §3），本 run 内存集合同步更新（后续同名调用 guard 直接 allow，不再过挂起）。

### 3. 持久化

- `SessionRecord.approved_tools`（JSON list）+ Alembic 迁移；`approve_session` 时 DB update + run 内存集合双写；guard 构造时从 SessionRecord 装载（按请求重建 agent 的现有限定下天然恢复）。

### 4. API

- `POST /api/sessions/{id}/confirmations/{confirmation_id}`，body `{decision: "approve" | "approve_session" | "deny"}`；鉴权 = 会话 owner；幂等：已结算的 confirmation_id 返回当前结算结果（不 409，前端重试友好）；未知 id → 404。
- 会话详情响应新增 `pending_confirmations` 列表（防御事件丢失，前端重连可从详情恢复弹窗）。
- 确认与拒绝动作写审计（audit_logger，复用工具执行审计口径）。

### 5. SSE 与通知

- `confirmation_requested` 事件载荷：`confirmation_id / tool_name / message / tool_call_id`；进 RunEventLog（重放支持）+ NotificationHub 推送（复用 run-status 通知口径，类型区分）。
- 结算后发 `confirmation_resolved` 事件（`decision`），前端据此撤下弹窗。

### 6. webui

- 全局通知（徽标/弹窗入口，复用完成 toast 通道样式）+ 会话视图内联确认卡（工具名、message、三按钮：仅本次 / 本会话内放行 / 拒绝）。
- 重连恢复：详情 `pending_confirmations` + 事件重放双保险；`confirmation_resolved` / 状态终态撤卡。

## 任务拆分（每任务独立提交，conventional commits）

- **T1 前置**：guardrails 计划 T1-T3 落地（`confirm` 词汇 + tool_call 派发 + 循环决策分支）；本计划 T2 起步的硬依赖。
- **T2 运行时**：`ConfirmationGuard` + RunManager pending 表 + 循环 confirm 分支（挂起/恢复/deny 合成）+ 审计。单测：三分支结算、同批串行、已批准集合短路、cancel 打断。
- **T3 持久化**：`SessionRecord.approved_tools` + 迁移 + 装载/双写。单测：重建后集合恢复。
- **T4 API**：confirmations 端点（鉴权/幂等/404）+ 详情 pending 字段。路由测试。
- **T5 webui**：确认卡 + 全局通知 + 重连恢复 + resolved 撤卡。node 测试：事件处理、重连恢复、三按钮状态。
- **T6 收尾**：`uv run pytest` + `npm test`/`build` + 真机验收（配名单工具触发 → 挂起 → 三种决定各验一次；断线重连恢复弹窗；`/api/stop` 终止挂起 run；重启后挂起 run = interrupted）+ 文档（`AGENTS.md` §5.3 运行时段落、`docs/operations/` 若有运维口径）+ 实施状态追加。

## 明确不做（本期）

- 自动超时拒绝（对齐结论 2）；遗忘挂起占 FIFO 位的风险文档化，`/api/stop` 兜底。
- 逐调用粒度 / admin 全局免确认；批量确认合并。
- 域声明确认名单（走 guardrails T8 域 guard 机制后续）。
- 确认名单的工具参数级条件（如"仅 write 到 X 目录需确认"）——那是 PathPolicyGuard 的领地。

## 验收标准

- 名单内工具触发挂起：SSE 事件、通知、弹窗三者可达；三分支决定行为正确；`approve_session` 后同名工具本会话不再询问（含会话重载后）。
- 断线重连：pending 弹窗可恢复（详情字段或事件重放）；已决定的弹窗被 `confirmation_resolved` 撤下。
- 挂起中 `/api/stop` 正常终止；进程重启后该 run 为 interrupted；审计留痕。
- `uv run pytest` 全绿；每任务独立提交；偏差记录于本文件。

## 实施状态（滚动更新）

- 2026-09-02：需求对齐完成（对齐结论 4 条），方案成文；依赖 guardrails 计划先行，未排期。
- 2026-09-02（批准后当日）：**T1-T6 全部落地**（9311bf7 → 915415e）。要点与偏差：
  - 运行时：ConfirmationGuard（tool_call 层）+ 循环 confirm 分支（批准放行/拒绝合成 `confirmation_denied`/无 handler fail-closed）+ RunManager pending Future 桥接 + `confirmation_requested`/`confirmation_resolved` run-log 事件（SSE 重放天然支持）。
  - 持久化偏差：`SessionRecord.approved_tools` 走 **JSON 文件存储，无需 Alembic 迁移**（方案按 DB 表假设，侦察结论修正——会话本就存 JSON 文件）。
  - API：`POST /api/sessions/{id}/confirmations/{cid}`（owner-only、幂等、approve_session 合并持久化）+ 会话详情 `pendingConfirmations`（重连恢复）。
  - webui：ConfirmationCard 三按钮（仅本次/本会话放行/拒绝）挂 ChatArea 与输入区之间；乐观撤卡失败回滚；详情预填 + 事件重放双保险。
  - **偏差：NotificationHub 推送未实现**（v1：确认只关乎当前会话标签页——SSE 重放 + 详情字段已覆盖，侧栏徽标挂起期间保持 running）；批量确认按计划逐个串行。
