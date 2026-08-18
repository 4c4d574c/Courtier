# 用户消息复制 + 编辑重发 实现计划

> **For agentic workers:** 按 Phase 推进，Task 内 Step 用 `- [x]` 跟踪，逐 Task 约定式提交。实施中发现与计划的偏差，记录到文末「实施记录」章节。

**Goal:** 前端用户消息气泡支持复制与编辑；编辑确认后联动后端撤销该轮（及之后所有轮次）的问答上下文并重新生成回答，之前轮次上下文完整保留。
**Architecture:** 复用 SSE `GET /api/sessions` 端点新增 `editTurn` 参数；后端 `truncate_session_to_turn` 原子截断 messages_json / 轮次数据 / steps / 会话树 / artifact 快照；前端用户气泡加 hover 工具栏与内联编辑，本地乐观截断后走既有 SSE 流式流程。
**Tech Stack:** FastAPI + JSON 文件会话存储（SessionRecord）；Vue 3 + TS + scoped CSS。
**关联文档:** 需求核对结论见本文件「背景与现状问题」；会话存储结构见 `courtier/agent/api/models.py` SessionRecord。

## 需求确认（2026-08-18 与用户核对，全部拍板）

1. 用户气泡 hover 显示「复制」「编辑」按钮；复制拷贝纯文本。
2. 任意一轮用户消息可编辑（内联编辑框，确认即重发，无"只改不发"）。
3. 编辑第 N 轮重发：级联撤销第 N 轮及其后所有轮次，从第 N-1 轮结束状态继续。
4. 被撤销内容彻底删除（界面 + 持久化存储，物理截断，不留版本导航）。
5. 编辑只改文字；带文件的轮次重发自动沿用原文件（fileName 透传，session 级 `file_id` 不动，强制重解析有 LRU 缓存兜底）。
6. 发生过上下文压缩的会话：不提供编辑入口并提示原因（压缩后历史是摘要，无法按轮干净切分）。
7. 运行中会话禁止编辑：前端隐藏入口，后端 409。

## 背景与现状问题

| # | 问题 / 事实 | 证据位置 | 阶段 |
|---|-------------|----------|------|
| 1 | 用户气泡只有纯文本，无复制/编辑按钮；全前端无 clipboard 代码 | `webui/src/components/chat/UserMessage.vue:1-14` | 2 |
| 2 | 后端无消息级编辑接口；fork/rewind 只动树指针不改 messages_json | `agent/api/services/session_service.py:71-140` | 1 |
| 3 | LLM 上下文唯一来源是 messages_json 平坦数组；真实用户消息判定 = `role=="user" 且 source is None`（排除 reminder/inline/hint 注入） | `agent/core/context_manager.py:780-788`、`agent/api/services/stream_service.py:89-115` | 1 |
| 4 | 轮次结构权威来源是 `turn_messages`/`turn_step_starts`/`turn_conclusions`；与 messages_json 双冗余，编辑需同步截断 | `agent/api/models.py:283-285,325-362` | 1 |
| 5 | 文件内容不进 user 消息：session 级 `file_id` + 强制 `parse_document` 首调用注入；截断 messages_json 不影响文件可用性 | `agent/api/routes/sessions.py:171-208`、`agent/agents/base.py:561-589` | 1 |
| 6 | artifact_snapshot 是整店累加 dump，无 per-turn 分界，无法原生回滚 | `agent/artifacts/store.py:289-388` | 1 |
| 7 | 压缩判定可靠：`context_state.has_compacted/compact_count`；但 `to_detail_dict` 不暴露，恢复的历史会话前端看不到压缩痕迹 | `agent/core/context_manager.py:73-83,154-184`、`agent/api/models.py:364-405` | 1/2 |
| 8 | 续轮端点无 running 防护（运行中重发会互相覆盖 active_tasks） | `agent/api/routes/sessions.py:145-181` | 1 |
| 9 | 会话树节点 = 全量消息前缀快照（flat dict + children id 列表），无删除节点 API；压缩会话被禁编辑后前缀性质成立，可按消息数 ≤ 截断长度裁剪 | `agent/core/conversation_tree.py:42-74,196-231`、`agent/core/state.py:330-352` | 1 |
| 10 | 前端 hover 揭示/内联编辑可参照 ChatSidebar meatballs 菜单与重命名 input | `webui/src/components/chat/ChatSidebar.vue:125-201,606-641` | 2 |

## 文件职责

| 文件 | 职责 | 操作 | 阶段 |
|------|------|------|------|
| `courtier/agent/api/services/session_service.py` | 新增 `truncate_session_to_turn` | 修改 | 1.1 |
| `courtier/agent/api/session_store.py` | `add_turn` 支持可选 fileName；新字段读写 | 修改 | 1.1/1.2 |
| `courtier/agent/api/models.py` | SessionRecord 增 `turn_artifact_snapshots`；`to_detail_dict` 增 `contextCompacted` | 修改 | 1.2 |
| `courtier/agent/api/services/stream_service.py` | 每轮 finalize 同步追加 per-turn artifact 快照 | 修改 | 1.2 |
| `courtier/agent/api/routes/sessions.py` | SSE 端点新增 `editTurn` 参数 + 校验 + 截断调用 | 修改 | 1.3 |
| `tests/agent/api/test_session_edit.py`（新） | 截断/校验/路由测试 | 新增 | 1.1-1.3 |
| `webui/src/types/chat.ts` / `types/agent.ts` | `ChatUserMessageItem.turnIndex`、`Session.contextCompacted` | 修改 | 2.1 |
| `webui/src/utils/chatMessages.ts` / `utils/toolCalls.ts` | user item 写 turnIndex；normalizeSession 映射 contextCompacted | 修改 | 2.1 |
| `webui/src/components/chat/UserMessage.vue` | hover 工具栏（复制/编辑）+ 内联编辑态 | 修改 | 2.2 |
| `webui/src/components/chat/{ChatMessage,ChatArea,ChatLayout}.vue` | props/事件穿透 | 修改 | 2.2 |
| `webui/src/composables/useAgentSession.ts` | 新增 `editAndResend(turnIndex, text)` | 修改 | 2.3 |
| `webui/src/api/client.ts` | createEventSource 支持 editTurn | 修改 | 2.3 |
| `webui/src/views/HomeView.vue` | 接线编辑提交 | 修改 | 2.3 |
| `webui/src/constants/messages.ts` | 新文案 | 修改 | 2.2 |

## Phase 1：后端截断与端点

### Task 1.1: truncate_session_to_turn + 单测

**Files:** `courtier/agent/api/services/session_service.py`、`courtier/agent/api/session_store.py`、`tests/agent/api/test_session_edit.py`（新）
**依赖:** 无

- [ ] **Step 1:** session_store `add_turn` 增可选 `fileName` 参数（编辑轮透传原 fileName 用）；`update`/`_load` 支持新字段 `turn_artifact_snapshots`（默认 []）。
- [ ] **Step 2:** session_service 新增 `truncate_session_to_turn(record, turn_index) -> dict[str, Any]`（返回 update kwargs）：messages_json 按"第 N 条真实 user 消息"（role==user 且 source is None）下标截断并对齐 tool 边界；turn_messages/turn_step_starts 截到前 N 项；turn_conclusions 截到 `min(N, len)`；steps 截到 `turn_step_starts[N]`；thoughts 按 `turn_index < N` 过滤；tree_json 删"消息数 > 截断长度"的链尾节点并把 current_node_id 移到最深存活节点；context_state 重置默认；session 级 conclusion 回退到 turn_conclusions[N-1]。
- [ ] **Step 3:** 单测覆盖：多轮会话截中间轮/首轮/末轮；含 reminder/inline/hint 注入消息的定位正确性；截断点 tool 边界无孤儿；steps/tree 同步截断；N 越界抛 ValueError。

### Task 1.2: per-turn artifact 快照历史 + contextCompacted 暴露

**Files:** `courtier/agent/api/models.py`、`courtier/agent/api/services/stream_service.py`、`courtier/agent/api/services/session_service.py`
**依赖:** Task 1.1

- [ ] **Step 1:** SessionRecord 增 `turn_artifact_snapshots: list[str]`；stream_service 每轮结束写 artifact_snapshot 时同步追加一份到历史。
- [ ] **Step 2:** truncate 时 artifact_snapshot 回滚到历史[N-1]（N=0 则清空为 None）并截断历史到前 N 项；历史缺失（legacy 会话）时保留现状快照不动。
- [ ] **Step 3:** `to_detail_dict` 增 `contextCompacted`（解析 context_state 的 has_compacted/compact_count）。
- [ ] **Step 4:** 单测：finalize 后历史对齐轮数；截断回滚精确性；legacy（无历史）回退；detail 字段真值。

### Task 1.3: SSE 端点 editTurn 接线

**Files:** `courtier/agent/api/routes/sessions.py`
**依赖:** Task 1.1、1.2

- [ ] **Step 1:** `GET /api/sessions` 增可选 `editTurn: int | None`；仅在 sessionId 续轮分支生效。
- [ ] **Step 2:** 校验顺序：会话存在/归属 → status 非 running 且不在 active_tasks（409）→ 未压缩（409，中文 detail）→ `0 ≤ editTurn < len(turn_messages)`（400）→ 调 truncate → add_turn 透传旧轮 fileName。
- [ ] **Step 3:** 路由级测试：正常截断重发 200；running 409；压缩 409；越界 400；无 sessionId 带 editTurn 忽略或 400。

## Phase 2：前端交互

### Task 2.1: 类型与数据映射

**Files:** `webui/src/types/chat.ts`、`webui/src/types/agent.ts`、`webui/src/utils/chatMessages.ts`、`webui/src/utils/toolCalls.ts`
**依赖:** Task 1.2（detail 字段）

- [ ] **Step 1:** `ChatUserMessageItem` 增 `turnIndex: number`；buildUserItem 写入；`Session` 增 `contextCompacted?: boolean`；normalizeSession/restoreSession 映射（缺省 false）。
- [ ] **Step 2:** 前端测试脚本补断言（turnIndex 编码、contextCompacted 映射）。

### Task 2.2: UserMessage 工具栏 + 内联编辑 + 事件穿透

**Files:** `webui/src/components/chat/UserMessage.vue`、`ChatMessage.vue`、`ChatArea.vue`、`ChatLayout.vue`、`webui/src/constants/messages.ts`
**依赖:** Task 2.1

- [ ] **Step 1:** UserMessage 增 props `turnIndex`、`canEdit`（非 running 且未压缩）；hover 工具栏（opacity 模式 + 触屏兜底）放复制/编辑图标按钮；复制走 navigator.clipboard + "已复制"瞬态反馈。
- [ ] **Step 2:** 编辑态：气泡换内联 textarea（autoResize、Enter 确认 / Shift+Enter 换行 / Esc 取消 / isComposing 保护）+ 确认/取消按钮；确认 emit `edit-submit { turnIndex, text }`。
- [ ] **Step 3:** 事件逐级穿透到 HomeView；压缩会话隐藏编辑按钮并 tooltip 提示；运行中隐藏。
- [ ] **Step 4:** 文案进 constants/messages.ts；样式遵循既有气泡/工具栏风格。

### Task 2.3: editAndResend + 端点接线

**Files:** `webui/src/composables/useAgentSession.ts`、`webui/src/api/client.ts`、`webui/src/views/HomeView.vue`
**依赖:** Task 1.3、2.2

- [ ] **Step 1:** client.ts createEventSource 支持 `editTurn`。
- [ ] **Step 2:** useAgentSession 新增 `editAndResend(turnIndex, text)`：本地 turns 截到前 N 项并重算派生状态（steps/conclusion/currentTurnIndex/compactions 清理），乐观 push 新轮（保留原 fileName 展示），建立 SSE。
- [ ] **Step 3:** HomeView 接线 edit-submit；isRunning/compacted 双保险拦截。
- [ ] **Step 4:** 前端测试：editAndResend 截断逻辑、URL 参数携带、compacted 拒绝。

## Phase 3：收尾

- [ ] 勾选本计划全部 Step，补「实施记录」（含实测结论与偏差）。
- [ ] 全量回归：`uv run pytest -m "not integration"`；`cd webui && npm test && npm run build`。
- [ ] 真实会话手工验证（编辑中间轮/首轮带文件/压缩会话/运行中四种场景）。

## 提交切分（约定式，逐 Task 一提交）

1. `docs(plans): user message edit-resend implementation plan`（Task 0）
2. `feat(agent): truncate session to turn for edit-resend`（Task 1.1）
3. `feat(agent): per-turn artifact snapshots + contextCompacted exposure`（Task 1.2）
4. `feat(agent): editTurn param on session SSE endpoint`（Task 1.3）
5. `feat(webui): turnIndex/contextCompacted plumbing`（Task 2.1）
6. `feat(webui): user bubble copy/edit toolbar with inline editor`（Task 2.2）
7. `feat(webui): editAndResend flow wiring`（Task 2.3）
8. `docs(plans): tick edit-resend plan, record implementation`（Phase 3）

## 总验收

- 编辑任意轮重发后：`GET /sessions/{id}` 只剩前 N-1 轮 + 新轮；下一轮续轮 messages_json 不含被撤销内容（含 tool 调用记录）。
- 编辑带文件的首轮：新轮仍显示文件 chip，重发后 agent 能重新解析原文件。
- 压缩会话/运行中会话：前端无编辑入口；直接调 API 带 editTurn 返回 409。
- 刷新页面重载会话，被撤销轮次不复活。

## 明确不做的事

- 助手气泡不加复制/编辑按钮（本次只作用户气泡）。
- 不做版本导航/分叉保留（彻底删除）。
- 编辑不支持更换附件、不支持"只改不发"。
- 不改动压缩机制本身；不为 legacy 会话补建 artifact 快照历史。
- 不处理 active_domains 按轮回放（超集无害）。

## 实施记录

（待实施后补记）
