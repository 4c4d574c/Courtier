# Courtier Agent Runtime 前端适配计划

> 本文档描述 Courtier 后端在 Pi 架构迁移后新增/变更的 SSE 事件类型，以及前端需要做的适配工作。
>
> **实施状态**：P0/P1 SSE 事件适配、fork/rewind API 客户端、会话树分支历史 UI、工具版本废弃标记展示均已落地。

---

## 1. 设计原则

1. **向后兼容**：旧前端不处理新事件类型时不应报错；新增事件类型均为“可忽略”。
2. **事件优先**：前端不再依赖 `state.transition` 的每一个细节，而是按事件类型分发给不同 UI 组件。
3. **调试与生产分离**：调试面板显示全部事件；普通用户界面只展示业务相关事件。

---

## 2. 当前 SSE 事件类型（已有）

| SSE type | 来源 | 含义 |
|---|---|---|
| `session` | `StreamService` | 会话开始 |
| `think` | `SSEAdapter.on_step` | 思考阶段 |
| `act` | `SSEAdapter.on_step` | 执行工具阶段 |
| `observe` | `SSEAdapter.on_step` | 观察结果阶段 |
| `token` | `SSEAdapter.on_token` | reasoning token |
| `conclusion_token` | `SSEAdapter.on_content_token` | 最终结论 token |
| `tool_start` | `SSEAdapter.on_tool_start` | 工具开始 |
| `tool_progress` | `SSEAdapter.on_tool_progress` | 工具进度 |
| `tool_result` | `SSEAdapter.on_tool_result` | 工具结果 |
| `usage` | `SSEAdapter._handle_usage` | token 统计 |
| `subagent_*` | `SSEAdapter.on_subagent_event` | 子代理事件族 |
| `complete` / `stopped` / `error` | `StreamService` | 会话终态 |

---

## 3. 新增 SSE 事件类型

### 3.1 `guard_triggered` ✅ 已落地

来源：`SSEAdapter._dispatch_event` 处理 `guard.triggered` 事件。

```json
{
  "type": "guard_triggered",
  "layer": "input",
  "guardName": "SensitiveInputGuard",
  "action": "block",
  "reason": "..."
}
```

**前端行为**：
- `action=block`：在聊天区显示红色警告卡片；
- `action=log`：显示黄色记录卡片；
- `action=allow`：显示绿色放行卡片。

**实现文件**：
- `webui/src/types/agent.ts`：`AgentEvent` / `RuntimeEvent` 类型扩展
- `webui/src/composables/sessionEventHandlers.ts`：事件捕获到 `session.guardEvents`
- `webui/src/utils/chatMessages.ts`：将 `guardEvents` 转换为 `ChatGuardItem`
- `webui/src/components/chat/GuardMessage.vue`：警告卡片组件
- `webui/src/components/chat/ChatMessage.vue`：渲染 `guard` 类型消息

### 3.2 `hint_injected` ✅ 已落地

来源：`SSEAdapter._dispatch_event` 处理 `hint.injected` 事件。

```json
{
  "type": "hint_injected",
  "hintType": "terminal_ready",
  "content": "[系统提示] 以下业务工具..."
}
```

**前端行为**：
- 事件捕获到 `session.hintEvents`；
- `DebugPanel` 在“提示注入”区域展示 `hintType` 与 `text`；
- 普通用户界面当前不显示。

### 3.3 `model_selected` / `model_fallback` ✅ 已落地

来源：`SSEAdapter._dispatch_event` 处理 `model.selected` / `model.fallback`。

```json
{
  "type": "model_selected",
  "model": "qwen3.6-27b",
  "backend": "openai",
  "strategy": "primary"
}
```

```json
{
  "type": "model_fallback",
  "model": "qwen3.6-27b",
  "backend": "local_backup",
  "reason": "timeout"
}
```

**前端行为**：
- `session.modelEvents` 记录模型选择/降级事件；
- `ChatHeader` 检测最近一次 `model_fallback`，在顶部模型名旁显示黄色“备用模型”标签。

**实现文件**：
- `webui/src/components/chat/ChatHeader.vue`：展示 `latestFallback`
- `webui/src/views/HomeView.vue` / `ChatLayout.vue`：透传 `modelEvents`

### 3.4 `loop_completed` ✅ 已落地

来源：`SSEAdapter._dispatch_event` 处理 `loop.completed`。

```json
{
  "type": "loop_completed",
  "status": "completed",
  "terminationReason": "...",
  "totalSteps": 5
}
```

**前端行为**：事件捕获到 `session.loopCompleted`；最终结论与 token 统计仍由 `complete` 事件驱动，`loopCompleted` 在调试面板中展示。

### 3.5 调试面板 ✅ 已落地

`DebugPanel` 整合 `guardEvents`、`hintEvents`、`modelEvents`、`loopCompleted` 以及会话分支历史，通过 `ChatHeader` 的调试图标按钮展开/收起。

**实现文件**：
- `webui/src/components/chat/DebugPanel.vue`：调试面板组件
- `webui/src/components/chat/ChatHeader.vue`：调试面板开关
- `webui/src/components/chat/ChatLayout.vue`：集成调试面板与事件透传

---

## 4. 会话树 UI

### 4.1 后端支持 ✅ 已落地

- `POST /sessions/{id}/fork`：从当前节点创建分支；
- `POST /sessions/{id}/rewind`：回退到指定节点；
- 两个端点都返回该节点的 messages，前端可直接用于重新生成。

### 4.2 API 客户端 ✅ 已落地

- `webui/src/api/client.ts` 新增 `api.forkSession(sessionId, nodeId?, reason?)` 与 `api.rewindSession(sessionId, nodeId)`。

### 4.3 分支历史 UI ✅ 已落地

1. **分支历史侧边栏**：
   - `ConversationTreePanel` 以树状图展示当前会话的节点；
   - 当前节点高亮；
   - 每个节点提供“分支”与“回退”按钮。

2. **Fork 操作**：
   - 在调试面板的会话分支中点击“分支”；
   - 调用 `POST /sessions/{id}/fork` 生成新分支；
   - 成功后通过 `api.loadSession` 刷新当前会话状态。

3. **Rewind 操作**：
   - 在调试面板的会话分支中点击“回退”；
   - 调用 `POST /sessions/{id}/rewind` 回到指定节点；
   - 成功后通过 `api.loadSession` 刷新当前会话状态。

4. **状态同步**：
   - `SessionRecord.to_detail_dict()` 返回 `treeJson` 与 `currentNodeId`；
   - `useAgentSession.forkSession` / `rewindSession` 调用 API 后重新加载会话；
   - 后续 SSE 流基于新的 `current_node_id` 继续追加。

**实现文件**：
- `courtier/courtier/agent/api/models.py`：`SessionRecord.to_detail_dict()` 追加 `treeJson` / `currentNodeId`
- `webui/src/types/agent.ts`：`ConversationTree` / `ConversationTreeNode` / `Session` 类型扩展
- `webui/src/components/chat/ConversationTreePanel.vue`：分支历史面板
- `webui/src/components/chat/ConversationTreeNode.vue`：递归树节点
- `webui/src/composables/useAgentSession.ts`：`forkSession` / `rewindSession`
- `webui/src/views/HomeView.vue`：处理 `fork-session` / `rewind-session` 事件

---

## 5. 工具版本化 UI ✅ 已落地

- 工具 schema 中 `description` / `skillDescription` 可能包含 `[DEPRECATED]` 标记；
- `ToolCardCollapsed` 解析该标记，在工具卡片标题旁展示 `DEPRECATED` 徽章，并高亮卡片边框；
- 当存在 `(use <replacement>)` 信息时，徽章 `title` 提示替换工具；
- 普通用户界面仍展示业务结果，废弃标记仅作视觉提示。

**实现文件**：
- `webui/src/utils/toolCalls.ts`：`isDeprecatedTool` / `parseDeprecatedReplacement`
- `webui/src/components/ToolCardCollapsed.vue`：废弃徽章渲染
- `webui/src/components/ToolCard.vue`：`.tool-card--deprecated` 类绑定
- `webui/src/styles/components.css`：废弃样式

---

## 6. 事件处理兼容性

建议前端 `handleEvent` 函数采用 switch-default 忽略未知事件：

```typescript
function handleEvent(event: ServerSentEvent) {
  switch (event.type) {
    case "token": return handleToken(event);
    case "tool_result": return handleToolResult(event);
    case "guard_triggered": return handleGuardTriggered(event);
    // ...
    default:
      // 未知类型：调试日志，不抛错
      if (isDebugMode()) console.debug("Unhandled SSE event", event);
  }
}
```

---

## 7. 优先级

| 优先级 | 事项 | 状态 |
|---|---|---|
| P0 | `guard_triggered` 交互提示 | ✅ 已落地 |
| P0 | `loop_completed` 事件捕获 | ✅ 已落地 |
| P1 | `model_selected` / `model_fallback` 顶部标签展示 | ✅ 已落地 |
| P1 | 会话树分支历史 UI | ✅ 已落地 |
| P2 | `hint_injected` 调试面板 | ✅ 已落地 |
| P2 | 工具版本废弃标记展示 | ✅ 已落地 |
