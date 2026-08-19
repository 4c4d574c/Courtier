# 后台任务队列(run 与连接解耦 + 断线续看) 实现计划

> **For agentic workers:** 按 Phase 推进,Task 内 Step 用 `- [x]` 跟踪,逐 Task 约定式提交。实施中发现与计划的偏差,记录到文末「实施记录」章节。

**Goal:** 会话运行(一个 turn)作为独立后台任务,生命周期与 SSE 连接解耦:退出页面/刷新/切换会话后未结束的任务继续运行;回到仍在运行的任务时,主界面以"快照 + 完整事件重放 + live"的形式无缝续看流式输出;配套任务队列可见性(实时徽标/完成提醒)与每用户并发上限排队。
**Architecture:** 引入 `AgentRun`/`RunManager`(run 注册表,取代 `app.state.active_tasks`);`SSEAdapter` 演进为 `RunRecorder`(唯一持久化执行者,写出带单调 seq 的 run 事件日志);SSE 连接退化为零副作用的 `LiveTailer`(只读日志:重放尾部 + live);SessionRecord 增加 `event_seq` 水位线保证"快照+重放"不重不漏;新增 `GET /api/sessions/{id}/events` attach 路由与 `GET /api/events` 全局状态通道。
**Tech Stack:** FastAPI + asyncio(单 worker 进程内实现,不引入 Redis/celery);JSON 文件会话存储(SessionRecord);Vue 3 + TS(原生 EventSource,GET + httpOnly cookie)。
**关联文档:** 需求与设计讨论结论见本文件「需求确认」「架构设计」;Pi 迁移背景见 `courtier/docs/architecture/pi-architecture-migration-plan.md`(EventBus 章节);会话存储结构见 `courtier/agent/api/models.py` SessionRecord。

## 需求确认(2026-08-19 与用户核对,全部拍板)

1. 会话运行与 SSE 连接解耦:断开连接只是 detach 观察者,任务继续;显式停止(`POST /api/stop`)才是取消。
2. 回到运行中任务时采用**完整事件重放**(非快照对齐):错过的中间事件按序补齐,体验与不断线一致。
3. 并发控制采用**每用户并发上限**(默认 3,超出入队 FIFO 排队),另设全局兜底上限(默认 20)。
4. 后台任务状态变化通过**全局事件通道**(`GET /api/events`)推送,侧栏徽标实时更新、非当前会话完成时弹 toast;不做轮询。
5. 服务重启:内存任务丢失可接受,启动时把 `running/queued` 会话标记 `interrupted`;不做自动重跑。
6. 参数默认值(均可配置):重放全速倾泻(不按原速回放动画);日志上限 5 万事件/8MB 每 run;终态宽限期 10 分钟。

## 架构设计

```
GET /api/sessions?task=…        ──start──┐
GET /api/sessions/{id}/events   ──attach─┤
                                       ▼
              ┌──────────── RunManager (app.state) ────────────┐
              │ per-user FIFO 队列 + 并发计数 + 全局兜底上限       │
              │ AgentRun: asyncio.Task + session级EventBus        │
              │          + RunRecorder + RunEventLog              │
              └──────┬──────────────────────────┬───────────────┘
                     ▼                          ▼
        agent.run(…, event_bus=run.bus)   RunRecorder(唯一实例)
                     │                     ├─ 持久化副作用 → SessionStore
                     ▼                     │   (方法级 event_seq 打点)
              EventBus(每 run)            └─ RunEventLog(seq 单调递增,
                     │                        边界对齐驱逐, 宽限期)
                     ▼                          ▲
               RunRecorder ◄────────────────────┘(唯写者)
                                          LiveTailer ×0..N(每连接一个,
                                          只读日志: since 重放 + live tail)
GET /api/events ◄── NotificationHub ◄── RunManager 状态迁移回调
```

**关键不变式:**

- **I1 单写者**:事件日志只由 RunRecorder 写;持久化副作用只发生在 RunRecorder 实时路径;重放绝不重放副作用(日志存的是最终 SSE payload 转写,不重过转换逻辑)。
- **I2 水位线**:`快照(event_seq=W) + 日志中 seq>W 的全部事件 ≡ 完整状态`。保证手段:每次 SessionStore 内容变更与水位推进在**同一次记录变更**中完成(store 方法级 `event_seq` 参数);recorder 按"预留 seq → 落库(打点) → 按 seq 入日志"的顺序处理带持久化的事件;seq 空洞(预留后异常)由读取端跳过。
- **I3 边界驱逐**:日志超限时只驱逐到"安全边界"(step/turn 开始处)为止——边界之前的内容已在 SessionStore 落盘,`快照+尾部` 恒可无损拼接;水位已被驱逐出日志时返回 `resync` 标记,前端重拉快照重新对齐。
- **I4 连接无关**:run 终态落盘(status/conclusion/finalize_turn_conclusion/token 注入)全部在 run 侧完成,与是否有观察者无关;终态后 run 保留宽限期再清理,迟到的 attach 仍能拿到完整重放(含终态事件)。

**决策记录(讨论中否决的备选):** 快照对齐方案(内存 O(当前步骤)但丢中间动画)被用户否决,选完整重放;侧栏轮询被否决,选全局事件通道;重启自动重跑被否决(重跑语义复杂:LLM 非确定性/副作用/费用)。

## 背景与现状问题

| # | 问题 / 事实 | 证据位置 | 阶段 |
|---|-------------|----------|------|
| 1 | SSE 消费者断开时 `finally` 显式 cancel runner,"断开即终止"是刻意设计 | `agent/api/services/stream_service.py:435-449` | 1 |
| 2 | 终态落盘一半在消费者循环里(status/conclusion/finalize_turn_conclusion/终止事件 token 注入);无消费者时任务跑完 status 永远停在 running | `stream_service.py:406-434` | 1 |
| 3 | runner 与消费者共享 `Queue(maxsize=1000)`:adapter 侧满则丢,但 runner 收尾用 `await queue.put` ——无消费者时长任务在收尾处永久阻塞(今天靠 cancel 规避) | `stream_service.py:223,357,392`、`sse_adapter.py:828-833` | 1 |
| 4 | EventBus 每请求新建、只挂一个订阅者、无任何回放/缓冲;SubAgent 事件绕过 bus 走 `on_subagent_event` 直连回调 | `stream_service.py:224,285`、`core/event_bus.py:110-145` | 1 |
| 5 | SSEAdapter 双职责:逐事件持久化副作用(steps/tools/verdicts/thoughts)+ SSE 转发,持久化跟着"每请求"走,与连接可插拔直接冲突 | `sse_adapter.py:32-57,347,447,674,705,725,754` | 1 |
| 6 | 原生 EventSource 断线重连会**原样重发含 task 的 URL**;续轮分支无 running 守卫 → 重连即重复 `add_turn` 重复跑一轮(既有潜在缺陷,本次顺带修复) | `webui/src/api/client.ts:363-380`、`agent/api/routes/sessions.py:145-214`(仅 editTurn 分支有守卫 `:164-165`) | 1 |
| 7 | 服务崩溃/重启后 running 会话永远显示 running,启动 lifespan 无清扫 | `agent/api/app.py:61-100` | 1 |
| 8 | 前端 `restoreSession` 只加载快照从不重开 SSE;切会话/卸载 HomeView 即关连接,流式状态即丢 | `webui/src/composables/useAgentSession.ts:194-225`、`views/HomeView.vue:191-222` | 2 |
| 9 | 侧栏会话列表只在 mount 拉一次,状态徽标长期陈旧;无任何后台活动通知 UI | `HomeView.vue:212-215`、`components/chat/ChatSidebar.vue:150-154` | 3 |
| 10 | 无并发上限:`active_tasks` 只防同一会话重复运行(且续轮分支未用),无每用户/全局限制 | `app.py:142`、`routes/control.py:59-114` | 4 |
| 11 | 会话/事件无 MySQL 表,存储为内存缓存 + JSON 文件;部署单 worker,应用无 Redis(仅 Langfuse 用)——后台任务必须进程内实现 | `agent/api/session_store.py:21-38`、`main.py:69-79`、`Dockerfile:56` | 全局约束 |

## 文件职责

| 文件 | 职责 | 操作 | 阶段 |
|------|------|------|------|
| `agent/api/services/run_event_log.py`(新) | `RunEventLog`:seq 预留/追加、边界标记、边界对齐驱逐、`replay_after(seq)`、live tail 读取器、seal | 新增 | 1.1 |
| `courtier/config.py` | 新设置:`max_runs_per_user`/`max_total_runs`/`run_log_max_events`/`run_log_max_bytes`/`run_grace_seconds` | 修改 | 1.1/1.4/4.1 |
| `agent/api/models.py` | SessionRecord 增 `event_seq`、状态枚举增 `queued`/`interrupted`;`to_detail_dict` 暴露 `eventSeq` | 修改 | 1.2/4.1 |
| `agent/api/session_store.py` | 内容变更方法增可选 `event_seq` 参数,同一次记录变更内打点(只前进不回退) | 修改 | 1.2 |
| `agent/api/sse_adapter.py` | `SSEAdapter` → `RunRecorder`:构造收 `RunEventLog`,`_emit_sse` 写日志(含 `id: seq` 行),store 调用带打点,终态事件生成(含 token 注入) | 修改(更名) | 1.3 |
| `agent/api/services/run_manager.py`(新) | `AgentRun`/`RunManager`:start/attach/stop/stop_all、runner 主体迁移、终态落盘、宽限期清理、启动清扫、排队(4.1) | 新增 | 1.4/4.1 |
| `agent/api/services/stream_service.py` | 删除 runner/queue/consumer;保留 `reconstruct_state` 等 helper;新增 `stream_run(run, since)` 连接侧生成器 | 修改(大幅缩减) | 1.4 |
| `agent/api/app.py` | `app.state.run_manager` 取代 `active_tasks`;lifespan 启动清扫(running/queued→interrupted)+ 关停取消全部 run | 修改 | 1.4 |
| `agent/api/routes/sessions.py` | 发起路由接 RunManager(已运行 409 / 同 task 重连转 attach);新 `GET /sessions/{id}/events`(since/Last-Event-ID);列表叠加实时状态 | 修改 | 1.5 |
| `agent/api/routes/control.py` | stop 改 `RunManager.stop`(运行中取消 + 排队移除);admin stop-all | 修改 | 1.5/4.1 |
| `agent/api/services/notification_hub.py`(新) | 每连接队列的轻量 hub + RunManager 状态迁移回调 | 新增 | 3.1 |
| `agent/api/routes/events.py`(新) | `GET /api/events` 全局通道(cookie 鉴权 + 限流) | 新增 | 3.1 |
| `webui/src/types/agent.ts` | `AgentEvent` 增 `queued`/`resync`;Session 状态类型扩展 | 修改 | 2.1 |
| `webui/src/api/client.ts` | `attachSessionEvents(sessionId, since)`、`createGlobalEventsChannel()` | 修改 | 2.1/3.2 |
| `webui/src/composables/useAgentSession.ts` | restoreSession 后 running 自动 attach;attach 失败重拉快照;`resync` 处理;`queued` 事件处理 | 修改 | 2.2/4.2 |
| `webui/src/composables/useRunEvents.ts`(新) | App 级单例全局通道连接;侧栏状态源;toast 触发 | 新增 | 3.2 |
| `webui/src/components/chat/ChatSidebar.vue` | 徽标实时化(接 useRunEvents);排队位置徽标 | 修改 | 3.2/4.2 |
| `webui/src/components/chat/InputArea.vue` 等运行态组件 | queued 态展示与停止排队 | 修改 | 4.2 |
| `tests/agent/api/test_run_event_log.py`(新)、`test_run_manager.py`(新) | 新模块单测 | 新增 | 1.1/1.4 |
| `tests/agent/api/test_sse_adapter.py`、`test_stream_service.py`、`test_event_bus_sse_integration.py`、`test_session_store.py`、`test_session_edit.py` | 随重构适配 + 新断言 | 修改 | 1.2-1.5 |
| `webui/scripts/*` 前端测试脚本 | attach 事件序列 / resync / queued 断言 | 修改 | 2.3/4.2 |

## Phase 1:后端 —— run 与连接解耦 + 事件日志

### Task 1.1: RunEventLog + 配置项

**Files:** `agent/api/services/run_event_log.py`(新)、`courtier/config.py`、`tests/agent/api/test_run_event_log.py`(新)
**依赖:** 无

- [x] **Step 1:** config 增 `run_log_max_events: int = 50000`、`run_log_max_bytes: int = 8_388_608`、`run_grace_seconds: int = 600`(带 env 别名)。
- [x] **Step 2:** `RunEventLog` 实现:`reserve() -> seq`(单调递增预分配)与 `append(payload, seq=None)`(无 seq 则自动分配;payload 为 dict,预渲染 `data:` 行);`mark_boundary(seq)`(记录安全边界);超限驱逐——回退到最旧安全边界之前整体丢弃,维护 `first_seq` 与字节数,单步事件超限兜底保留最新并置 `truncated`;`replay_after(seq) -> list[(seq, line)]`(跳过 seq 空洞;`seq < first_seq` 时返回 `RESYNC` 哨兵);`reader(since)` 异步迭代器(先重放后 live,await 新事件,连接侧消费);`seal()`(终态封口,读取器自然结束);多读取器并发支持(每读取器独立等待队列,`put_nowait`+满则丢弃该读取器并标记需 resync)。
- [x] **Step 3:** SSE 行格式:`id: {seq}\ndata: {json}\n\n`(前端只读 data,向后兼容)。
- [x] **Step 4:** 单测:seq 连续性与空洞跳过;边界驱逐正确性(边界前内容被驱、边界后完整);超限 truncated;replay_after 各窗口;seal 后读取器收尾;并发读取器互不影响;字节数核算。

### Task 1.2: event_seq 水位线

**Files:** `agent/api/models.py`、`agent/api/session_store.py`、`tests/agent/api/test_session_store.py`
**依赖:** 无(与 1.1 并行)

- [x] **Step 1:** SessionRecord 增 `event_seq: int = 0`;`to_detail_dict` 暴露 `eventSeq`;状态注释补充 `queued`/`interrupted`(枚举扩展在 4.1 落地,此处仅注释与 detail 映射)。
- [x] **Step 2:** session_store 内容变更方法(`add_step`/`finalize_step`/`add_tool_info`/`add_thought`/`set_verdict`/`add_turn`/`update`)增可选 `event_seq: int | None`,在同一次内存记录变更中写入,且只前进不回退(`max(old, new)`)。
- [x] **Step 3:** 单测:打点与内容同变更可见(无中间态);水位不回退;legacy 记录缺省 0。

### Task 1.3: SSEAdapter → RunRecorder

**Files:** `agent/api/sse_adapter.py`、`tests/agent/api/test_sse_adapter.py`
**依赖:** Task 1.1、1.2

- [x] **Step 1:** 类更名 `RunRecorder`,构造参数以 `run_log: RunEventLog` 取代 `queue`;`_emit_sse` 改为写日志(带 seq 的 `id:` 行);删除 `asyncio.QueueFull` 丢弃逻辑(日志自身有驱逐)。
- [x] **Step 2:** 带持久化的事件按"预留 seq → store 调用(带 event_seq) → 按 seq 入日志"顺序改造:`on_token`(add_thought)、`on_tool_result`(add_tool_info)、`_handle_think`/`_handle_think_tool_calls`(add_step)、`_handle_observe`(set_verdict/finalize_step);纯转发事件直接 append。SubAgent 直连回调 `on_subagent_event` 维持直连进 recorder(不回发 bus),经 `_emit_sse` 入日志。
- [x] **Step 3:** 新增终态方法 `emit_terminal(kind, conclusion="")`:生成 `complete`/`stopped`/`error` SSE payload(token 计数注入沿用 `_inject_token_counts` 逻辑,移入本文件),写入日志后 `seal()`。`flush_verdict`/`start_listening`/`stop_listening`/`_check_pause` 语义不变。
- [x] **Step 4:** 单测适配:以 RunEventLog 断言输出(含 seq 与 id 行);打点顺序(先库后日志、seq 对齐);终态事件内容。

### Task 1.4: RunManager + runner 迁移 + app 接线

**Files:** `agent/api/services/run_manager.py`(新)、`agent/api/services/stream_service.py`、`agent/api/app.py`、`tests/agent/api/test_run_manager.py`(新)、`tests/agent/api/test_stream_service.py`、`tests/agent/api/test_event_bus_sse_integration.py`
**依赖:** Task 1.3

- [x] **Step 1:** `AgentRun`:session_id、user、状态(`running|completed|error|stopped`,Phase 4 前无 queued)、`asyncio.Task`、session 级 EventBus、RunRecorder、RunEventLog、created/finished 时间。
- [x] **Step 2:** `RunManager.start(spec) -> AgentRun`:spec 携带 `build` 闭包(执行时才 build_agent,为 4.1 排队延迟构建留位)与 task/上下文等;创建 run、`asyncio.create_task(runner)`;同 session 已有未终态 run 则抛冲突(路由转 409 或 attach)。
- [x] **Step 3:** runner 主体自 `generate_sse_stream` 迁移:compaction 状态恢复、artifact store 准备、model config、`agent.run(event_bus=run.bus, on_subagent_event=recorder.on_subagent_event, …)`、final_state 持久化(messages_json/artifact_snapshot/context_state/tree_json/active_domains);`is_new` 的 `session` 事件与 `initial→running` 提升移到 run 启动处(入日志,不再由连接首 chunk 驱动)。
- [x] **Step 4:** 终态路径全部 run 侧完成:正常结束 → recorder.emit_terminal("complete") + `status=completed/conclusion/finalize_turn_conclusion`;`CancelledError` → stopped 落盘 + emit_terminal("stopped");异常 → error 落盘(trace_id)+ emit_terminal("error");终态后按 `run_grace_seconds` 懒清理(attach/get 时检查过期即弃)。
- [x] **Step 5:** `attach(session_id, since)` → (run, reader) 或 None(不在运行且过宽限期);`stop(session_id)`(含 `plugin_system.cancel_pending()` 前置,行为对齐现 `control.py:63-67`)/`stop_all()`;`has_active(session_id)`(edit-resend 守卫改用)。
- [x] **Step 6:** `stream_service` 缩减为 `stream_run(run, since)`:建读取器、先 `session` 兜底事件(attach 场景日志无 session 事件时补发)、yield `id:/data:` 行直至 seal;删除 queue/runner/consumer/取消逻辑。
- [x] **Step 7:** `app.py`:`app.state.run_manager = RunManager(...)`,删除 `active_tasks`;lifespan 启动时清扫 SessionStore 中 `running`→`interrupted`(补 finished_at);关停时取消全部 run 任务。
- [x] **Step 8:** 单测:断开后 run 继续并完整落盘(模拟连接早退);终态落盘不再依赖消费者(修复现状问题 #2/#3);同 session 重复 start 冲突;stop 全路径;宽限期 attach 拿到含终态的完整重放;启动清扫;无消费者时收尾不阻塞。

### Task 1.5: 路由接线(发起防重复 + attach 端点 + stop)

**Files:** `agent/api/routes/sessions.py`、`agent/api/routes/control.py`、`tests/agent/api/test_session_attach.py`(新)、`test_session_edit.py`
**依赖:** Task 1.4

- [ ] **Step 1:** 发起路由:构造 spec(build 闭包 + 参数)→ `RunManager.start` → `stream_run(run, since=0)`;同 session 已有活跃 run:带相同 task 的重连(原生 EventSource 重发)转 `attach`,不同 task 则 409(修复现状问题 #6 重复轮次缺陷);edit-resend 守卫由 `active_tasks` 改 `run_manager.has_active`(扩展 queued 在 4.1)。
- [ ] **Step 2:** 新路由 `GET /api/sessions/{session_id}/events?since=int`:归属校验(get_owned);`Last-Event-ID` 请求头优先于 query 参数;run 存在(含宽限期)→ `stream_run(run, since)`;不存在 → 404(前端退回快照);限流 30/min,响应头与主 SSE 一致。
- [ ] **Step 3:** `control.py` stop/stop-all 改走 `RunManager`(保留 plugin cancel_pending 前置与所有权语义)。
- [ ] **Step 4:** 会话列表响应叠加实时状态:RunManager 活跃/宽限期内的会话以内存状态覆盖 store 滞后值(queued 于 4.1 生效)。
- [ ] **Step 5:** 路由级测试(ASGITransport + mock agent,沿用 test_session_edit 模式):发起后断开 run 继续;attach 重放与 since 对齐(水位不变式 I2 端到端);Last-Event-ID 续流;404/409 分支;重复 task 重连不产生重复轮次;stop。

## Phase 2:前端 —— 恢复会话自动续看

### Task 2.1: API 与类型

**Files:** `webui/src/api/client.ts`、`webui/src/types/agent.ts`、`webui/src/constants/messages.ts`
**依赖:** Task 1.5(端点可用)

- [ ] **Step 1:** `attachSessionEvents(sessionId, since)`:EventSource(`/api/sessions/{id}/events?since=`,withCredentials),`onmessage`/`onerror` 形态与现有 createEventSource 一致。
- [ ] **Step 2:** `AgentEvent` 联合类型增 `{type:"queued"; position:number}`、`{type:"resync"}`;Session status 类型增 `queued`/`interrupted`。
- [ ] **Step 3:** 新文案(排队中/已中断/续看失败回落快照等)进 constants/messages.ts。

### Task 2.2: restoreSession 自动续接 + 兜底

**Files:** `webui/src/composables/useAgentSession.ts`、`views/HomeView.vue`
**依赖:** Task 2.1

- [ ] **Step 1:** restoreSession 后若快照 `status==="running"`(4.1 后含 queued):以快照 `eventSeq` 为 since 调 attach;事件走既有 handler 管线,零新对齐逻辑(重放即正常事件流)。
- [ ] **Step 2:** attach 失败(404,run 刚结束/过宽限期)→ 重拉一次快照并按终态渲染;`resync` 事件 → 关连接 → 重拉快照 → 以新 eventSeq 重新 attach(一次,防循环)。
- [ ] **Step 3:** 断线重连:沿用原生 EventSource 重连(Last-Event-ID 由浏览器自动携带,服务端续流);重连耗尽逻辑复用现有 MAX_RECONNECTS 框架。
- [ ] **Step 4:** `queued` 事件:置会话排队态(输入区/状态条展示,复用运行态样式),收到后续事件自然切运行;`interrupted` 快照按终态渲染 + 提示可重新发起。
- [ ] **Step 5:** 切走即 disconnect 语义保持(现在只是 detach,无需改);多标签页同看一个运行中会话随 attach 天然支持。

### Task 2.3: 前端测试

**Files:** `webui/scripts/*`(新增或扩展)
**依赖:** Task 2.2

- [ ] **Step 1:** 脚本断言:快照 eventSeq → attach URL 参数;queued/resync 事件分支;attach 404 回落重拉;重放事件序列与直接流式等价(同一 handler 管线)。

## Phase 3:全局事件通道与任务可见性

### Task 3.1: NotificationHub + GET /api/events

**Files:** `agent/api/services/notification_hub.py`(新)、`agent/api/routes/events.py`(新)、`agent/api/app.py`、`tests/agent/api/test_notification_hub.py`(新)
**依赖:** Task 1.4(RunManager 状态回调点)

- [ ] **Step 1:** NotificationHub:每连接独立队列(满则丢最旧,状态事件低频可忽略);RunManager 在 `queued/started/completed/error/stopped` 迁移点回调广播,负载 `{type:"run_status", sessionId, status, queuePosition?, conclusion?, tokensIn/Out?}`;按 user 过滤只推本人会话。
- [ ] **Step 2:** `GET /api/events`:cookie 鉴权 + 限流(10/min);心跳注释行防代理超时;连接断开即除名;无重放(前端重连后重拉列表对齐)。
- [ ] **Step 3:** 单测:状态迁移广播到达;user 隔离;断连清理。

### Task 3.2: 前端实时徽标 + toast

**Files:** `webui/src/composables/useRunEvents.ts`(新)、`App.vue`、`components/chat/ChatSidebar.vue`、`views/HomeView.vue`、适当的通知组件(新增轻量 toast)
**依赖:** Task 3.1、Task 2.2

- [ ] **Step 1:** useRunEvents:登录后建立 App 级单例通道,登出/401 断开;对外暴露按 sessionId 订阅状态变化的响应式源 + 当前用户活跃 run 计数。
- [ ] **Step 2:** ChatSidebar 徽标接 useRunEvents 实时刷新(running 动画/queued 位置/completed/error),替换 mount 单次拉取的陈旧快照;通道断线重连成功后重拉一次列表。
- [ ] **Step 3:** toast:非当前打开会话的 completed/error 弹出(标题 + 结论摘要截断 + 点击跳转);当前会话不弹(界面本身在流式)。
- [ ] **Step 4:** 前端测试脚本:事件驱动徽标状态机、toast 触发条件。

## Phase 4:每用户并发上限与排队

### Task 4.1: RunManager 排队后端

**Files:** `agent/api/services/run_manager.py`、`agent/api/routes/sessions.py`、`agent/api/routes/control.py`、`agent/api/models.py`、`courtier/config.py`、`tests/agent/api/test_run_manager.py`
**依赖:** Task 1.5(建议在 Phase 2/3 验收后)

- [ ] **Step 1:** config 增 `max_runs_per_user: int = 3`(0=不限)、`max_total_runs: int = 20`。
- [ ] **Step 2:** 入队:start 时若用户运行数达上限或全局兜底满 → run 置 `queued` 入全局等待队列(入队顺序),会话 status 落 `queued`,日志首事件 `{type:"queued", position}`;spec 的 build 闭包延迟到真正启动才执行。
- [ ] **Step 3:** 准入:任一 run 终态后按入队顺序扫描等待队列,启动第一个满足"该用户运行数 < 上限 且 全局运行数 < 兜底"的 run(不严格队头阻塞);`queued→running` 迁移经 NotificationHub 广播,日志衔接 `session`/正常事件。
- [ ] **Step 4:** stop 扩展:queued run 直接出队置 `stopped`(从未执行,无副作用清理);stop-all 同;edit-resend 409 守卫扩展到 queued;排队位置变化广播(queuePosition 递减)。
- [ ] **Step 5:** models/session_store:status 枚举正式增 `queued`;启动清扫覆盖 `queued`→`interrupted`;列表/详情映射。
- [ ] **Step 6:** 单测:上限触发入队与 FIFO 准入(含跨用户穿插、队头不阻塞);build 延迟执行;排队中 stop;位置广播;重启清扫 queued。

### Task 4.2: 前端排队体验

**Files:** `webui/src/components/chat/InputArea.vue`、`ChatStatusMessage.vue`、`ChatSidebar.vue`、`composables/useAgentSession.ts`、`webui/scripts/*`
**依赖:** Task 4.1、Task 3.2

- [ ] **Step 1:** 发起 SSE 首事件可能为 `queued`(位置 N):主界面展示"排队中,前面还有 N 个任务"(复用运行态布局,停止按钮可停排队)。
- [ ] **Step 2:** 侧栏 queued 徽标 + 位置;queued→running 时本地状态切换(queued 事件与通道事件双来源,以事件先到者为准)。
- [ ] **Step 3:** 前端测试脚本:排队事件流、停止排队、位置递减。

## Phase 5:收尾

- [ ] 勾选本计划全部 Step,补「实施记录」(含实测结论与偏差)。
- [ ] 全量回归:`uv run pytest -m "not integration"`;`cd webui && npm test && npm run build`。
- [ ] 真实会话手工验证清单:① 发起任务→关标签页→后台跑完;② 发起→切会话→切回,流式续看与不断线一致;③ 刷新页面回到运行中会话;④ 运行中断网重连(Last-Event-ID 续流);⑤ 后台任务 stop;⑥ 第 4 个任务排队→槽位释放自动启动;⑦ 侧栏徽标实时 + 完成 toast;⑧ 重启服务→interrupted。
- [ ] 文档同步:根 `AGENTS.md` §5.3(运行时架构)、`CLAUDE.md`、`courtier/CLAUDE.md` 中 active_tasks/断开即停的描述更新为 RunManager/后台任务模型。

## 提交切分(约定式,逐 Task 一提交)

1. `docs(plans): background task queue implementation plan`(本文件)
2. `feat(agent): bounded run event log with boundary-aligned eviction`(Task 1.1)
3. `feat(agent): event_seq watermark on session records`(Task 1.2)
4. `refactor(agent): SSEAdapter becomes RunRecorder writing the run event log`(Task 1.3)
5. `feat(agent): RunManager detaches run lifecycle from SSE connections`(Task 1.4)
6. `feat(agent): session events attach endpoint with replay`(Task 1.5)
7. `feat(webui): auto re-attach streaming on session restore`(Task 2.1-2.3)
8. `feat(agent): global run-events notification channel`(Task 3.1)
9. `feat(webui): live sidebar run status and completion toasts`(Task 3.2)
10. `feat(agent): per-user run queueing with FIFO admission`(Task 4.1)
11. `feat(webui): queued run badges and stop-while-queued`(Task 4.2)
12. `docs(plans): tick background task queue plan, record implementation`(Phase 5)

## 总验收

- **后台续跑**:发起任务后关标签页/切会话/刷新,后端 run 继续;结束后会话 `completed`、结论/轮次/messages_json 完整落盘(与在线跑完完全一致)。
- **无缝续看**:回到运行中会话,界面 = 快照 + 错过事件重放 + live,与从未断线等价(含 SubAgent 树、工具卡片、token 流、终态);网络闪断走 Last-Event-ID 续流,不产生重复轮次。
- **停止语义**:SSE 断开不终止任务;`POST /api/stop` 终止运行中任务(stopped 落盘);排队中任务可停止(4.1 后)。
- **水位线不变式**:任意时刻 `GET /sessions/{id}` 快照与其 `eventSeq` + `events?since=该值` 重放,拼出的前端状态无丢失、无重复步骤/工具卡片。
- **排队**:每用户第 4 个任务入队等待,FIFO 准入,槽位释放自动启动,位置可见;全局兜底上限生效。
- **通道**:侧栏徽标实时跟随后台任务状态;非当前会话完成/出错弹 toast;通道断线重连后自动对齐。
- **重启**:running/queued 会话标记 interrupted,界面可见并可重新发起;无永久 running 脏状态。
- **回归**:后端全量测试通过,前端测试脚本 + `vue-tsc && vite build` 通过。

## 明确不做的事

- 多 worker / 分布式部署支持(RunManager 进程内实现;接口留演进余地,不引入 Redis)。
- 服务重启后自动重跑或排队持久化恢复(接受丢失,标记 interrupted)。
- 排队优先级、抢占、暂停单个任务(全局 pause/resume 语义维持现状不动)。
- 重放按原速回放动画(全速倾泻);全局通道转发 token 级内容(仅状态迁移)。
- 会话/事件存储迁移 MySQL(JSON SessionStore 维持);浏览器 Web Notification 推送。
- SubAgent 直连回调改走 EventBus(维持 recorder 直收,出口统一入日志)。
- 不为 legacy 历史会话补建 event_seq(运行中会话必然由新代码创建,天然有水位)。

## 实施记录

(待实施后填写:逐 Task 提交哈希、实测结论、与计划的偏差及拍板理由。)
