# Courtier WebUI Chat Redesign — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rewrite the Courtier webui chat interface as a Kimi-style light-theme chat with a left sidebar, collapsible thinking cards, file cards, and a right-side file-preview drawer, while keeping the existing SSE session backend unchanged.

**Architecture:** Add a new `components/chat/` component tree and two small composables (`useChatMessages`, `useFilePreview`). Pure message-building logic lives in `utils/chatMessages.ts` so it can be unit-tested with the existing Node test harness. `HomeView.vue` switches from `Terminal.vue` to the new `ChatLayout.vue` and keeps the uploaded-file object URLs for drawer previews.

**Tech Stack:** Vue 3 (Composition API, `<script setup>`), TypeScript, Vite, CSS custom properties (light/dark `data-theme`), Node `assert` test scripts.

---

## File Structure

| File | Responsibility |
|------|----------------|
| `src/types/chat.ts` | New chat-specific types (`ChatMessageItem`, `ChatFileRecord`). |
| `src/types/agent.ts` | Add optional `fileId` to `Message`. |
| `src/utils/chatMessages.ts` | Pure helpers: `buildChatMessages`, `deriveConversationTitle`, `fileMimeType`. |
| `src/composables/useChatMessages.ts` | Computed wrapper around `buildChatMessages`. |
| `src/composables/useFilePreview.ts` | Drawer open/close state. |
| `src/styles/theme.css` | Light/dark CSS variables and `html[data-theme]` overrides. |
| `src/main.ts` | Import `theme.css` after `style.css`. |
| `src/constants/messages.ts` | Add chat UI strings. |
| `src/components/chat/ChatLayout.vue` | Root layout: sidebar + main area. |
| `src/components/chat/ChatSidebar.vue` | Left navigation: new session, history, admin, user. |
| `src/components/chat/ChatHeader.vue` | Top bar: summary title, theme toggle, user menu. |
| `src/components/chat/ChatArea.vue` | Scrollable message stream + empty state. |
| `src/components/chat/ChatMessage.vue` | Switch on message item type. |
| `src/components/chat/UserMessage.vue` | User bubble. |
| `src/components/chat/AssistantMessage.vue` | Assistant reply with `StreamingMarkdown`. |
| `src/components/chat/ThinkingCard.vue` | Collapsible thinking card. |
| `src/components/chat/FileCard.vue` | File card that emits `preview`. |
| `src/components/chat/ChatStatusMessage.vue` | Error / stopped notice. |
| `src/components/chat/InputArea.vue` | Bottom input bar with file attach. |
| `src/components/chat/FilePreviewDrawer.vue` | Right-side file preview drawer. |
| `src/composables/useAgentSession.ts` | Pass `fileId` into the user turn message. |
| `src/views/HomeView.vue` | Wire new composables and `ChatLayout`. |
| `scripts/test-chat-messages.mjs` | Unit tests for `chatMessages.ts` helpers. |
| `package.json` | Append new test script to the `test` command. |

---

### Task 1: Extend types for chat items and file tracking

**Files:**
- Create: `src/types/chat.ts`
- Modify: `src/types/agent.ts:86-91`

- [ ] **Step 1: Add chat item types**

Create `src/types/chat.ts`:

```typescript
export interface ChatUserMessageItem {
  type: "user";
  id: string;
  content: string;
}

export interface ChatAssistantMessageItem {
  type: "assistant";
  id: string;
  content: string;
}

export interface ChatThinkingItem {
  type: "thinking";
  id: string;
  content: string;
  isOpen: boolean;
}

export interface ChatFileItem {
  type: "file";
  id: string;
  name: string;
  url: string;
  mimeType: string;
}

export interface ChatErrorItem {
  type: "error";
  id: string;
  title: string;
  detail?: string;
}

export interface ChatStoppedItem {
  type: "stopped";
  id: string;
  title: string;
  detail?: string;
}

export type ChatMessageItem =
  | ChatUserMessageItem
  | ChatAssistantMessageItem
  | ChatThinkingItem
  | ChatFileItem
  | ChatErrorItem
  | ChatStoppedItem;

export interface ChatFileRecord {
  fileId: string;
  name: string;
  url: string;
}
```

- [ ] **Step 2: Add `fileId` to the backend message type**

Modify `src/types/agent.ts`:

```typescript
export interface Message {
  role: "user";
  text: string;
  fileName?: string;
  fileId?: string;
  timestamp: number;
}
```

- [ ] **Step 3: Commit**

```bash
git add src/types/chat.ts src/types/agent.ts
git commit -m "feat(chat): add chat item types and fileId to message"
```

---

### Task 2: Wire fileId through the session composable

**Files:**
- Modify: `src/composables/useAgentSession.ts:53-92`

- [ ] **Step 1: Accept and store `fileId` in the user turn**

In `src/composables/useAgentSession.ts`, change the `connect` function signature and the `Turn` creation block:

```typescript
function connect(task: string, fileId?: string, fileName?: string) {
  // ... existing disconnect/reconnect logic ...

  const turn: Turn = {
    message: {
      role: "user",
      text: task,
      fileId,
      fileName,
      timestamp: Date.now(),
    },
    steps: [],
  };
  session.turns.push(turn);
  state.currentTurn = session.turns[session.turns.length - 1];

  api
    .createEventSource({
      task,
      fileId: fileId || undefined,
      sessionId: currentSessionId.value || undefined,
    })
    // ... rest unchanged ...
}
```

- [ ] **Step 2: Commit**

```bash
git add src/composables/useAgentSession.ts
git commit -m "feat(chat): pass uploaded fileId through session turn"
```

---

### Task 3: Build pure chat-message helpers

**Files:**
- Create: `src/utils/chatMessages.ts`
- Test: `scripts/test-chat-messages.mjs`

- [ ] **Step 1: Write the failing test**

Create `scripts/test-chat-messages.mjs`:

```javascript
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { rmSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const scriptDir = dirname(fileURLToPath(import.meta.url));
const rootDir = resolve(scriptDir, "..");
const outDir = resolve(rootDir, ".tmp/chat-messages-test");

rmSync(outDir, { recursive: true, force: true });
mkdirSync(outDir, { recursive: true });

try {
  execFileSync(
    process.execPath,
    [
      resolve(rootDir, "node_modules/typescript/bin/tsc"),
      "--target",
      "ES2020",
      "--module",
      "ES2020",
      "--moduleResolution",
      "bundler",
      "--strict",
      "--skipLibCheck",
      "--outDir",
      outDir,
      "--rootDir",
      resolve(rootDir, "src"),
      resolve(rootDir, "src/utils/chatMessages.ts"),
    ],
    { cwd: rootDir, stdio: "inherit" },
  );

  const chatMessages = await import(
    pathToFileURL(resolve(outDir, "utils/chatMessages.js")).href
  );
  const { buildChatMessages, deriveConversationTitle, fileMimeType } =
    chatMessages;

  const baseSession = {
    id: "s1",
    task: "审核合同",
    modelName: "Claude",
    status: "completed" as const,
    turns: [],
    steps: [],
    thoughts: [],
    stats: { tokensIn: 0, tokensOut: 0, elapsed: 0 },
    createdAt: 1,
  };

  // Title from task
  assert.equal(deriveConversationTitle(baseSession), "审核合同");

  // Title fallback to first user message
  const titleSession = {
    ...baseSession,
    task: "",
    turns: [
      {
        message: { role: "user" as const, text: "检查格式", timestamp: 1 },
        steps: [],
      },
    ],
  };
  assert.equal(deriveConversationTitle(titleSession), "检查格式");

  // User + file + assistant
  const fileRecords = [
    { fileId: "f1", name: "notice.pdf", url: "blob://notice" },
  ];
  const fileSession = {
    ...baseSession,
    turns: [
      {
        message: {
          role: "user" as const,
          text: "审核这份通知",
          fileId: "f1",
          fileName: "notice.pdf",
          timestamp: 1,
        },
        steps: [],
        conclusion: "格式正确",
      },
    ],
  };
  const msgs = buildChatMessages(fileSession, fileRecords);
  assert.equal(msgs.length, 3);
  assert.equal(msgs[0].type, "user");
  assert.equal(msgs[1].type, "file");
  assert.equal((msgs[1] as any).url, "blob://notice");
  assert.equal((msgs[1] as any).mimeType, "application/pdf");
  assert.equal(msgs[2].type, "assistant");
  assert.equal((msgs[2] as any).content, "格式正确");

  // MIME helper
  assert.equal(fileMimeType("photo.jpg"), "image/jpeg");
  assert.equal(fileMimeType("doc.docx"), "application/octet-stream");

  console.log("chatMessages verification passed");
} finally {
  rmSync(outDir, { recursive: true, force: true });
}
```

Run:

```bash
cd /home/lmwl/Documents/docaudit/agent/courtier/webui
node scripts/test-chat-messages.mjs
```

Expected: `Module not found` / `buildChatMessages is not a function`.

- [ ] **Step 2: Implement `src/utils/chatMessages.ts`**

```typescript
import type { Session, Thought, Turn } from "../types/agent";
import type {
  ChatFileRecord,
  ChatMessageItem,
} from "../types/chat";
import { thoughtsForStep } from "./sessionUtils";
import { MESSAGES } from "../constants/messages";

export function deriveConversationTitle(session: Session): string {
  if (session.task?.trim()) {
    return truncate(session.task.trim(), 20);
  }
  const firstUserText = session.turns.find(
    (t) => t.message.text.trim(),
  )?.message.text;
  if (firstUserText) {
    return truncate(firstUserText.trim(), 20);
  }
  return "新会话";
}

function truncate(text: string, max: number): string {
  if (text.length <= max) return text;
  return text.slice(0, max) + "…";
}

function collectTurnThoughts(turn: Turn, allThoughts: Thought[]): Thought[] {
  const seen = new Set<number>();
  const result: Thought[] = [];
  for (let i = 0; i < turn.steps.length; i++) {
    for (const thought of thoughtsForStep(allThoughts, turn, i)) {
      if (!seen.has(thought.id)) {
        seen.add(thought.id);
        result.push(thought);
      }
    }
  }
  return result;
}

function findFileRecord(
  records: ChatFileRecord[],
  fileId?: string,
  fileName?: string,
): ChatFileRecord | undefined {
  if (fileId) {
    const byId = records.find((r) => r.fileId === fileId);
    if (byId) return byId;
  }
  if (fileName) {
    return records.find((r) => r.name === fileName);
  }
  return undefined;
}

export function fileMimeType(name: string): string {
  const ext = name.split(".").pop()?.toLowerCase();
  switch (ext) {
    case "pdf":
      return "application/pdf";
    case "jpg":
    case "jpeg":
      return "image/jpeg";
    case "png":
      return "image/png";
    case "gif":
      return "image/gif";
    case "bmp":
      return "image/bmp";
    case "tif":
    case "tiff":
      return "image/tiff";
    case "docx":
      return "application/vnd.openxmlformats-officedocument.wordprocessingml.document";
    default:
      return "application/octet-stream";
  }
}

export function buildChatMessages(
  session: Session,
  fileRecords: ChatFileRecord[],
): ChatMessageItem[] {
  const items: ChatMessageItem[] = [];
  const isRunning = session.status === "running";

  session.turns.forEach((turn, turnIndex) => {
    const baseId = `turn-${turnIndex}`;
    const isLastTurn = turnIndex === session.turns.length - 1;

    items.push({
      type: "user",
      id: `${baseId}-user`,
      content: turn.message.text,
    });

    if (turn.message.fileName) {
      const record = findFileRecord(
        fileRecords,
        turn.message.fileId,
        turn.message.fileName,
      );
      items.push({
        type: "file",
        id: `${baseId}-file`,
        name: turn.message.fileName,
        url: record?.url ?? "",
        mimeType: record ? fileMimeType(record.name) : "application/octet-stream",
      });
    }

    const thoughts = collectTurnThoughts(turn, session.thoughts);
    if (thoughts.length > 0) {
      items.push({
        type: "thinking",
        id: `${baseId}-thinking`,
        content: thoughts.map((t) => t.text).join("\n\n"),
        isOpen: true,
      });
    }

    const conclusion = turn.conclusion ?? session.conclusion;
    if (conclusion || (isRunning && isLastTurn)) {
      items.push({
        type: "assistant",
        id: `${baseId}-assistant`,
        content: conclusion ?? "",
      });
    }

    if (isLastTurn) {
      if (session.errorMessage) {
        items.push({
          type: "error",
          id: `${baseId}-error`,
          title: MESSAGES.SESSION_ERROR_TITLE,
          detail: session.errorMessage,
        });
      } else if (session.stopReason === "user" && !isRunning) {
        items.push({
          type: "stopped",
          id: `${baseId}-stopped`,
          title: MESSAGES.SESSION_STOPPED,
          detail: MESSAGES.SESSION_STOPPED_DETAIL,
        });
      }
    }
  });

  return items;
}
```

Note: `error`/`stopped` items are included in the `ChatMessageItem` union defined in `src/types/chat.ts`. `ChatMessage.vue` renders them with `ChatStatusMessage.vue`.

- [ ] **Step 3: Run tests**

```bash
node scripts/test-chat-messages.mjs
```

Expected: `chatMessages verification passed`.

- [ ] **Step 4: Commit**

```bash
git add src/utils/chatMessages.ts scripts/test-chat-messages.mjs
git commit -m "feat(chat): add chat message builders and title helper"
```

---

### Task 4: Wrap helpers in composables

**Files:**
- Create: `src/composables/useChatMessages.ts`
- Create: `src/composables/useFilePreview.ts`

- [ ] **Step 1: Create `useChatMessages`**

```typescript
import { computed, type Ref } from "vue";
import type { Session } from "../types/agent";
import type { ChatFileRecord, ChatMessageItem } from "../types/chat";
import { buildChatMessages, deriveConversationTitle } from "../utils/chatMessages";

export function useChatMessages(
  session: Session,
  fileRecords: Ref<ChatFileRecord[]>,
) {
  const messages = computed<ChatMessageItem[]>(() =>
    buildChatMessages(session, fileRecords.value),
  );
  const title = computed(() => deriveConversationTitle(session));
  return { messages, title };
}
```

- [ ] **Step 2: Create `useFilePreview`**

```typescript
import { ref } from "vue";
import type { ChatFileItem } from "../types/chat";

export function useFilePreview() {
  const isOpen = ref(false);
  const currentFile = ref<ChatFileItem | null>(null);

  function open(file: ChatFileItem) {
    currentFile.value = file;
    isOpen.value = true;
  }

  function close() {
    isOpen.value = false;
    currentFile.value = null;
  }

  return { isOpen, currentFile, open, close };
}
```

- [ ] **Step 3: Commit**

```bash
git add src/composables/useChatMessages.ts src/composables/useFilePreview.ts
git commit -m "feat(chat): add chat composables"
```

---

### Task 5: Add chat theme tokens

**Files:**
- Create: `src/styles/theme.css`
- Modify: `src/main.ts`

- [ ] **Step 1: Create theme CSS**

Create `src/styles/theme.css`:

```css
:root {
  --chat-bg-body: #f4f5f7;
  --chat-bg-card: #ffffff;
  --chat-bg-hover: #f3f4f6;
  --chat-text-primary: #111827;
  --chat-text-secondary: #6b7280;
  --chat-text-tertiary: #9ca3af;
  --chat-accent: #4f46e5;
  --chat-accent-hover: #4338ca;
  --chat-accent-soft: rgba(79, 70, 229, 0.1);
  --chat-border: #e5e7eb;
  --chat-shadow: 0 1px 3px rgba(0, 0, 0, 0.08);
  --chat-radius-sm: 6px;
  --chat-radius-md: 8px;
  --chat-radius-lg: 12px;
  --chat-sidebar-width: 260px;
  --chat-drawer-width: 480px;
}

[data-theme="dark"] {
  --chat-bg-body: #111827;
  --chat-bg-card: #1f2937;
  --chat-bg-hover: #374151;
  --chat-text-primary: #f9fafb;
  --chat-text-secondary: #9ca3af;
  --chat-text-tertiary: #6b7280;
  --chat-accent: #818cf8;
  --chat-accent-hover: #6366f1;
  --chat-accent-soft: rgba(129, 140, 248, 0.15);
  --chat-border: #374151;
  --chat-shadow: 0 1px 3px rgba(0, 0, 0, 0.3);
}

html[data-theme],
html[data-theme] body {
  background: var(--chat-bg-body);
  color: var(--chat-text-primary);
}

@media (prefers-reduced-motion: reduce) {
  html[data-theme] *,
  html[data-theme] *::before,
  html[data-theme] *::after {
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.01ms !important;
  }
}
```

- [ ] **Step 2: Import it in `src/main.ts`**

Add this line after the existing `import "./style.css"`:

```typescript
import "./styles/theme.css";
```

- [ ] **Step 3: Commit**

```bash
git add src/styles/theme.css src/main.ts
git commit -m "feat(chat): add light/dark chat theme tokens"
```

---

### Task 6: Add chat UI strings

**Files:**
- Modify: `src/constants/messages.ts`

- [ ] **Step 1: Append new entries**

Add these keys to `MESSAGES` (existing welcome/quick-task/loading keys are reused in components):

```typescript
  /** Chat UI */
  CHAT_NEW_SESSION: "新会话",
  CHAT_HISTORY_TITLE: "历史会话",
  CHAT_NO_HISTORY: "暂无历史会话",
  CHAT_SEND: "发送",
  CHAT_STOP: "停止",
  CHAT_UPLOADING: "上传中...",
  CHAT_ATTACH: "上传文档",
  CHAT_PLACEHOLDER: "输入审核任务描述，例如：审核这份通知的格式规范",
  CHAT_THINKING: "思考过程",
  CHAT_THINKING_ACTIVE: "分析中…",
  CHAT_MODEL: "模型",
  CHAT_THEME_TOGGLE: "切换主题",
  CHAT_TOGGLE_SIDEBAR: "展开/收起侧边栏",
  CHAT_SETTINGS: "个人设置",
  CHAT_LOGOUT: "退出登录",
  CHAT_USER_MANAGE: "用户管理",
  CHAT_APPROVALS: "注册审批",
  CHAT_PREVIEW_DOWNLOAD: "下载",
  CHAT_PREVIEW_UNSUPPORTED: "当前文件格式不支持浏览器预览，请下载后查看。",
```

- [ ] **Step 2: Commit**

```bash
git add src/constants/messages.ts
git commit -m "feat(chat): add chat UI message strings"
```

---

### Task 7: Create the sidebar

**Files:**
- Create: `src/components/chat/ChatSidebar.vue`

- [ ] **Step 1: Implement the component**

```vue
<template>
  <aside class="chat-sidebar" :class="{ 'chat-sidebar--open': isOpen }">
    <div class="chat-sidebar-header">
      <div class="chat-sidebar-logo">
        <span class="chat-sidebar-logo-mark">审</span>
        <span class="chat-sidebar-logo-text">SDTAgent</span>
      </div>
      <button
        class="chat-sidebar-new"
        type="button"
        @click="$emit('new-session')"
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M12 5v14M5 12h14" />
        </svg>
        {{ MESSAGES.CHAT_NEW_SESSION }}
      </button>
    </div>

    <div class="chat-sidebar-section">
      <h3 class="chat-sidebar-section-title">{{ MESSAGES.CHAT_HISTORY_TITLE }}</h3>
      <div v-if="loading" class="chat-sidebar-loading">{{ MESSAGES.LOADING }}</div>
      <div v-else-if="sessions.length === 0" class="chat-sidebar-empty">
        {{ MESSAGES.CHAT_NO_HISTORY }}
      </div>
      <ul v-else class="chat-sidebar-list">
        <li
          v-for="session in sessions"
          :key="session.id"
          class="chat-sidebar-item"
          @click="$emit('select', session.id)"
        >
          <span class="chat-sidebar-item-task">{{ session.task }}</span>
          <span class="chat-sidebar-item-meta">
            <span
              class="chat-sidebar-item-dot"
              :class="session.status"
            ></span>
            {{ formatDate(session.createdAt) }}
          </span>
          <button
            class="chat-sidebar-item-delete"
            type="button"
            @click.stop="$emit('delete', session.id)"
            :aria-label="MESSAGES.DELETE_CONFIRM"
          >
            ×
          </button>
        </li>
      </ul>
    </div>

    <div v-if="isAdmin" class="chat-sidebar-section">
      <h3 class="chat-sidebar-section-title">管理</h3>
      <nav class="chat-sidebar-nav">
        <router-link to="/admin/users" class="chat-sidebar-nav-item">
          {{ MESSAGES.CHAT_USER_MANAGE }}
        </router-link>
        <router-link to="/admin/approvals" class="chat-sidebar-nav-item">
          {{ MESSAGES.CHAT_APPROVALS }}
        </router-link>
      </nav>
    </div>

    <div class="chat-sidebar-footer">
      <router-link to="/profile" class="chat-sidebar-footer-item">
        {{ MESSAGES.CHAT_SETTINGS }}
      </router-link>
      <button class="chat-sidebar-footer-item" type="button" @click="$emit('logout')">
        {{ MESSAGES.CHAT_LOGOUT }}
      </button>
    </div>
  </aside>
</template>

<script setup lang="ts">
import { computed } from "vue";
import type { SessionSummary } from "../../types/agent";
import { MESSAGES } from "../../constants/messages";

interface Props {
  sessions: SessionSummary[];
  loading: boolean;
  isOpen: boolean;
  userRole?: string;
}

const props = defineProps<Props>();
defineEmits<{
  "new-session": [];
  select: [id: string];
  delete: [id: string];
  logout: [];
}>();

const isAdmin = computed(() => props.userRole === "admin");

function formatDate(ts: number): string {
  const d = new Date(ts);
  return `${d.getMonth() + 1}/${d.getDate()} ${d.getHours()}:${String(
    d.getMinutes(),
  ).padStart(2, "0")}`;
}
</script>

<style scoped>
.chat-sidebar {
  width: var(--chat-sidebar-width);
  flex-shrink: 0;
  background: var(--chat-bg-card);
  border-right: 1px solid var(--chat-border);
  display: flex;
  flex-direction: column;
  height: 100%;
  transition: transform 0.25s ease;
}

.chat-sidebar-header {
  padding: 16px;
  border-bottom: 1px solid var(--chat-border);
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.chat-sidebar-logo {
  display: flex;
  align-items: center;
  gap: 10px;
}

.chat-sidebar-logo-mark {
  width: 32px;
  height: 32px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: 2px solid var(--chat-accent);
  border-radius: var(--chat-radius-sm);
  color: var(--chat-accent);
  font-weight: 700;
  font-size: 16px;
}

.chat-sidebar-logo-text {
  font-size: 18px;
  font-weight: 600;
  color: var(--chat-text-primary);
}

.chat-sidebar-new {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 8px 12px;
  border-radius: var(--chat-radius-md);
  border: 1px solid var(--chat-border);
  background: var(--chat-bg-card);
  color: var(--chat-text-primary);
  cursor: pointer;
  transition:
    background 0.15s,
    border-color 0.15s;
}

.chat-sidebar-new:hover {
  background: var(--chat-bg-hover);
  border-color: var(--chat-accent);
}

.chat-sidebar-new svg {
  width: 16px;
  height: 16px;
}

.chat-sidebar-section {
  padding: 12px 12px 4px;
}

.chat-sidebar-section-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--chat-text-tertiary);
  text-transform: uppercase;
  letter-spacing: 0.06em;
  margin-bottom: 8px;
  padding: 0 4px;
}

.chat-sidebar-loading,
.chat-sidebar-empty {
  padding: 12px 4px;
  font-size: 14px;
  color: var(--chat-text-secondary);
}

.chat-sidebar-list {
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.chat-sidebar-item {
  position: relative;
  padding: 10px;
  border-radius: var(--chat-radius-md);
  cursor: pointer;
  transition: background 0.15s;
}

.chat-sidebar-item:hover {
  background: var(--chat-bg-hover);
}

.chat-sidebar-item-task {
  display: block;
  font-size: 14px;
  color: var(--chat-text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.chat-sidebar-item-meta {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--chat-text-tertiary);
  margin-top: 4px;
}

.chat-sidebar-item-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
}

.chat-sidebar-item-dot.completed {
  background: #22c55e;
}
.chat-sidebar-item-dot.running {
  background: #f59e0b;
}
.chat-sidebar-item-dot.error {
  background: #ef4444;
}

.chat-sidebar-item-delete {
  position: absolute;
  top: 6px;
  right: 6px;
  width: 22px;
  height: 22px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: none;
  background: transparent;
  color: var(--chat-text-tertiary);
  opacity: 0;
  cursor: pointer;
  border-radius: var(--chat-radius-sm);
  transition: opacity 0.15s;
}

.chat-sidebar-item:hover .chat-sidebar-item-delete {
  opacity: 1;
}

.chat-sidebar-item-delete:hover {
  background: rgba(239, 68, 68, 0.1);
  color: #ef4444;
}

.chat-sidebar-nav {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.chat-sidebar-nav-item {
  padding: 8px 10px;
  border-radius: var(--chat-radius-md);
  font-size: 14px;
  color: var(--chat-text-primary);
  text-decoration: none;
  transition: background 0.15s;
}

.chat-sidebar-nav-item:hover {
  background: var(--chat-bg-hover);
}

.chat-sidebar-footer {
  margin-top: auto;
  border-top: 1px solid var(--chat-border);
  padding: 8px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.chat-sidebar-footer-item {
  padding: 8px 10px;
  border-radius: var(--chat-radius-md);
  font-size: 14px;
  color: var(--chat-text-primary);
  text-decoration: none;
  text-align: left;
  background: transparent;
  border: none;
  cursor: pointer;
  transition: background 0.15s;
}

.chat-sidebar-footer-item:hover {
  background: var(--chat-bg-hover);
}

@media (max-width: 768px) {
  .chat-sidebar {
    position: fixed;
    left: 0;
    top: 0;
    bottom: 0;
    z-index: 100;
    transform: translateX(-100%);
  }
  .chat-sidebar--open {
    transform: translateX(0);
  }
}
</style>
```

- [ ] **Step 2: Commit**

```bash
git add src/components/chat/ChatSidebar.vue
git commit -m "feat(chat): add left navigation sidebar"
```

---

### Task 8: Create the header

**Files:**
- Create: `src/components/chat/ChatHeader.vue`

- [ ] **Step 1: Implement the component**

```vue
<template>
  <header class="chat-header">
    <button
      class="chat-header-menu"
      type="button"
      :title="MESSAGES.CHAT_TOGGLE_SIDEBAR"
      @click="$emit('toggle-sidebar')"
    >
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <line x1="3" y1="6" x2="21" y2="6" />
        <line x1="3" y1="12" x2="21" y2="12" />
        <line x1="3" y1="18" x2="21" y2="18" />
      </svg>
    </button>
    <h1 class="chat-header-title" :title="title">{{ title }}</h1>
    <div class="chat-header-right">
      <span v-if="modelName" class="chat-header-model">{{ modelName }}</span>
      <button
        class="chat-header-icon"
        type="button"
        :title="MESSAGES.CHAT_THEME_TOGGLE"
        @click="$emit('toggle-theme')"
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <circle cx="12" cy="12" r="5" />
          <path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42" />
        </svg>
      </button>
      <div class="chat-header-user" @click="menuOpen = !menuOpen">
        <span class="chat-header-user-name">{{ username || "用户" }}</span>
        <span class="chat-header-user-arrow">▾</span>
        <div v-if="menuOpen" class="chat-header-dropdown">
          <router-link to="/profile" class="chat-header-dropdown-item" @click.stop>
            {{ MESSAGES.CHAT_SETTINGS }}
          </router-link>
          <template v-if="isAdmin">
            <router-link to="/admin/users" class="chat-header-dropdown-item" @click.stop>
              {{ MESSAGES.CHAT_USER_MANAGE }}
            </router-link>
            <router-link to="/admin/approvals" class="chat-header-dropdown-item" @click.stop>
              {{ MESSAGES.CHAT_APPROVALS }}
            </router-link>
          </template>
          <button class="chat-header-dropdown-item" type="button" @click.stop="logout">
            {{ MESSAGES.CHAT_LOGOUT }}
          </button>
        </div>
      </div>
    </div>
  </header>
</template>

<script setup lang="ts">
import { ref, computed } from "vue";
import { MESSAGES } from "../../constants/messages";

interface Props {
  title: string;
  modelName?: string;
  username?: string;
  userRole?: string;
}

const props = defineProps<Props>();
const emit = defineEmits<{
  "toggle-sidebar": [];
  "toggle-theme": [];
  logout: [];
}>();

const menuOpen = ref(false);
const isAdmin = computed(() => props.userRole === "admin");

function logout() {
  menuOpen.value = false;
  emit("logout");
}
</script>

<style scoped>
.chat-header {
  height: 56px;
  flex-shrink: 0;
  background: var(--chat-bg-card);
  border-bottom: 1px solid var(--chat-border);
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 16px;
  gap: 12px;
}

.chat-header-menu {
  display: none;
  width: 36px;
  height: 36px;
  align-items: center;
  justify-content: center;
  border: none;
  background: transparent;
  color: var(--chat-text-secondary);
  border-radius: var(--chat-radius-md);
  cursor: pointer;
}

.chat-header-menu:hover {
  background: var(--chat-bg-hover);
  color: var(--chat-text-primary);
}

.chat-header-menu svg {
  width: 20px;
  height: 20px;
}

.chat-header-title {
  flex: 1;
  font-size: 16px;
  font-weight: 600;
  color: var(--chat-text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  margin: 0;
}

.chat-header-right {
  display: flex;
  align-items: center;
  gap: 12px;
}

.chat-header-model {
  font-size: 12px;
  color: var(--chat-text-tertiary);
  padding: 4px 8px;
  border: 1px solid var(--chat-border);
  border-radius: 999px;
}

.chat-header-icon {
  width: 36px;
  height: 36px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: none;
  background: transparent;
  color: var(--chat-text-secondary);
  border-radius: var(--chat-radius-md);
  cursor: pointer;
}

.chat-header-icon:hover {
  background: var(--chat-bg-hover);
  color: var(--chat-text-primary);
}

.chat-header-icon svg {
  width: 18px;
  height: 18px;
}

.chat-header-user {
  position: relative;
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 6px 10px;
  border-radius: var(--chat-radius-md);
  cursor: pointer;
  color: var(--chat-text-secondary);
}

.chat-header-user:hover {
  background: var(--chat-bg-hover);
}

.chat-header-user-name {
  font-size: 14px;
  color: var(--chat-text-primary);
}

.chat-header-user-arrow {
  font-size: 10px;
}

.chat-header-dropdown {
  position: absolute;
  top: 100%;
  right: 0;
  margin-top: 6px;
  min-width: 140px;
  background: var(--chat-bg-card);
  border: 1px solid var(--chat-border);
  border-radius: var(--chat-radius-md);
  box-shadow: var(--chat-shadow);
  overflow: hidden;
  z-index: 100;
}

.chat-header-dropdown-item {
  display: block;
  padding: 10px 14px;
  font-size: 14px;
  color: var(--chat-text-primary);
  text-decoration: none;
  text-align: left;
  background: transparent;
  border: none;
  width: 100%;
  cursor: pointer;
}

.chat-header-dropdown-item:hover {
  background: var(--chat-bg-hover);
}

@media (max-width: 768px) {
  .chat-header-menu {
    display: flex;
  }
  .chat-header-model {
    display: none;
  }
}
</style>
```

- [ ] **Step 2: Commit**

```bash
git add src/components/chat/ChatHeader.vue
git commit -m "feat(chat): add conversation header"
```

---

### Task 9: Create message sub-components

**Files:**
- Create: `src/components/chat/UserMessage.vue`
- Create: `src/components/chat/AssistantMessage.vue`
- Create: `src/components/chat/ThinkingCard.vue`
- Create: `src/components/chat/FileCard.vue`
- Create: `src/components/chat/ChatStatusMessage.vue`

- [ ] **Step 1: UserMessage.vue**

```vue
<template>
  <div class="user-message">
    <div class="user-message-bubble">
      <div class="user-message-text">{{ content }}</div>
    </div>
  </div>
</template>

<script setup lang="ts">
interface Props {
  content: string;
}
defineProps<Props>();
</script>

<style scoped>
.user-message {
  display: flex;
  justify-content: flex-end;
}

.user-message-bubble {
  max-width: 80%;
  background: var(--chat-accent);
  color: #fff;
  border-radius: var(--chat-radius-lg);
  border-bottom-right-radius: 4px;
  padding: 12px 16px;
  font-size: 15px;
  line-height: 1.6;
  box-shadow: var(--chat-shadow);
}
</style>
```

- [ ] **Step 2: AssistantMessage.vue**

```vue
<template>
  <div class="assistant-message">
    <div class="assistant-message-bubble">
      <StreamingMarkdown
        class="assistant-message-text"
        :content="content"
        :is-streaming="isStreaming"
      />
    </div>
  </div>
</template>

<script setup lang="ts">
import StreamingMarkdown from "../StreamingMarkdown.vue";

interface Props {
  content: string;
  isStreaming?: boolean;
}
defineProps<Props>();
</script>

<style scoped>
.assistant-message {
  display: flex;
  justify-content: flex-start;
}

.assistant-message-bubble {
  max-width: 90%;
  background: var(--chat-bg-card);
  border: 1px solid var(--chat-border);
  border-radius: var(--chat-radius-lg);
  padding: 14px 18px;
  font-size: 15px;
  line-height: 1.7;
  color: var(--chat-text-primary);
  box-shadow: var(--chat-shadow);
}
</style>
```

- [ ] **Step 3: ThinkingCard.vue**

```vue
<template>
  <div class="thinking-card">
    <div
      class="thinking-card-header"
      role="button"
      tabindex="0"
      :aria-expanded="expanded"
      @click="toggle"
      @keydown.enter.prevent="toggle"
      @keydown.space.prevent="toggle"
    >
      <span class="thinking-card-label">
        <span v-if="isStreaming" class="thinking-card-dot"></span>
        {{ MESSAGES.CHAT_THINKING }}
        <span v-if="isStreaming" class="thinking-card-badge">
          {{ MESSAGES.CHAT_THINKING_ACTIVE }}
        </span>
      </span>
      <span class="thinking-card-toggle">{{ expanded ? "收起 ▲" : "展开 ▼" }}</span>
    </div>
    <Transition name="expand">
      <div v-if="expanded" class="thinking-card-body">
        <StreamingMarkdown
          class="thinking-card-text"
          :content="content"
          :is-streaming="isStreaming"
        />
      </div>
    </Transition>
  </div>
</template>

<script setup lang="ts">
import { ref } from "vue";
import { MESSAGES } from "../../constants/messages";
import StreamingMarkdown from "../StreamingMarkdown.vue";

interface Props {
  content: string;
  isStreaming?: boolean;
  defaultOpen?: boolean;
}

const props = defineProps<Props>();
const expanded = ref(props.defaultOpen ?? true);

function toggle() {
  expanded.value = !expanded.value;
}
</script>

<style scoped>
.thinking-card {
  border: 1px solid var(--chat-border);
  border-radius: var(--chat-radius-md);
  background: var(--chat-bg-card);
  overflow: hidden;
  box-shadow: var(--chat-shadow);
}

.thinking-card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 14px;
  cursor: pointer;
  user-select: none;
  background: var(--chat-bg-hover);
}

.thinking-card-label {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 14px;
  font-weight: 600;
  color: var(--chat-text-primary);
}

.thinking-card-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--chat-accent);
  animation: pulse 1.4s ease-in-out infinite;
}

.thinking-card-badge {
  font-size: 12px;
  color: var(--chat-accent);
  background: var(--chat-accent-soft);
  padding: 2px 8px;
  border-radius: 999px;
}

.thinking-card-toggle {
  font-size: 12px;
  color: var(--chat-text-secondary);
}

.thinking-card-body {
  padding: 14px 16px;
  background: var(--chat-bg-card);
}

.thinking-card-text {
  font-size: 14px;
  color: var(--chat-text-secondary);
  line-height: 1.7;
}

@keyframes pulse {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.3;
  }
}
</style>
```

- [ ] **Step 4: FileCard.vue**

```vue
<template>
  <div
    class="file-card"
    :class="{ 'file-card--previewable': file.url }"
    @click="handleClick"
  >
    <div class="file-card-icon">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
        <polyline points="14 2 14 8 20 8" />
      </svg>
    </div>
    <div class="file-card-info">
      <span class="file-card-name">{{ file.name }}</span>
      <span v-if="!file.url" class="file-card-hint">历史文件，暂不支持预览</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import type { ChatFileItem } from "../../types/chat";

interface Props {
  file: ChatFileItem;
}

const props = defineProps<Props>();
const emit = defineEmits<{
  preview: [file: ChatFileItem];
}>();

function handleClick() {
  if (props.file.url) {
    emit("preview", props.file);
  }
}
</script>

<style scoped>
.file-card {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  padding: 10px 14px;
  border: 1px solid var(--chat-border);
  border-radius: var(--chat-radius-md);
  background: var(--chat-bg-card);
  color: var(--chat-text-primary);
  box-shadow: var(--chat-shadow);
  max-width: 80%;
  cursor: default;
  opacity: 0.7;
}

.file-card--previewable {
  cursor: pointer;
  opacity: 1;
}

.file-card--previewable:hover {
  border-color: var(--chat-accent);
}

.file-card-icon {
  width: 36px;
  height: 36px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: var(--chat-radius-sm);
  background: var(--chat-accent-soft);
  color: var(--chat-accent);
  flex-shrink: 0;
}

.file-card-icon svg {
  width: 20px;
  height: 20px;
}

.file-card-info {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.file-card-name {
  font-size: 14px;
  font-weight: 500;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.file-card-hint {
  font-size: 12px;
  color: var(--chat-text-tertiary);
}
</style>
```

- [ ] **Step 5: ChatStatusMessage.vue**

```vue
<template>
  <div class="status-message" :class="`status-message--${type}`">
    <span class="status-message-icon">{{ type === "error" ? "✕" : "⊘" }}</span>
    <div>
      <div class="status-message-title">{{ title }}</div>
      <div v-if="detail" class="status-message-detail">{{ detail }}</div>
    </div>
  </div>
</template>

<script setup lang="ts">
interface Props {
  type: "error" | "stopped";
  title: string;
  detail?: string;
}
defineProps<Props>();
</script>

<style scoped>
.status-message {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 12px 14px;
  border-radius: var(--chat-radius-md);
  border-left: 3px solid;
  background: var(--chat-bg-card);
  font-size: 14px;
}

.status-message--error {
  border-left-color: #ef4444;
}

.status-message--stopped {
  border-left-color: #f59e0b;
}

.status-message-icon {
  font-weight: 700;
  line-height: 1.4;
}

.status-message--error .status-message-icon {
  color: #ef4444;
}

.status-message--stopped .status-message-icon {
  color: #f59e0b;
}

.status-message-title {
  font-weight: 600;
  color: var(--chat-text-primary);
}

.status-message-detail {
  margin-top: 4px;
  color: var(--chat-text-secondary);
}
</style>
```

- [ ] **Step 6: Commit**

```bash
git add src/components/chat/UserMessage.vue src/components/chat/AssistantMessage.vue src/components/chat/ThinkingCard.vue src/components/chat/FileCard.vue src/components/chat/ChatStatusMessage.vue
git commit -m "feat(chat): add message sub-components"
```

---

### Task 10: Create the message dispatcher and chat area

**Files:**
- Create: `src/components/chat/ChatMessage.vue`
- Create: `src/components/chat/ChatArea.vue`

- [ ] **Step 1: ChatMessage.vue**

```vue
<template>
  <UserMessage v-if="item.type === 'user'" :content="item.content" />
  <AssistantMessage
    v-else-if="item.type === 'assistant'"
    :content="item.content"
    :is-streaming="isStreaming"
  />
  <ThinkingCard
    v-else-if="item.type === 'thinking'"
    :content="item.content"
    :is-streaming="isStreaming"
    :default-open="item.isOpen"
  />
  <FileCard
    v-else-if="item.type === 'file'"
    :file="item"
    @preview="$emit('preview', $event)"
  />
  <ChatStatusMessage
    v-else-if="item.type === 'error' || item.type === 'stopped'"
    :type="item.type"
    :title="item.title"
    :detail="item.detail"
  />
</template>

<script setup lang="ts">
import type { ChatMessageItem } from "../../types/chat";
import UserMessage from "./UserMessage.vue";
import AssistantMessage from "./AssistantMessage.vue";
import ThinkingCard from "./ThinkingCard.vue";
import FileCard from "./FileCard.vue";
import ChatStatusMessage from "./ChatStatusMessage.vue";

interface Props {
  item: ChatMessageItem;
  isStreaming?: boolean;
}

defineProps<Props>();
defineEmits<{
  preview: [file: Extract<ChatMessageItem, { type: "file" }>];
}>();
</script>
```

- [ ] **Step 2: ChatArea.vue**

```vue
<template>
  <div ref="containerRef" class="chat-area" aria-live="polite">
    <div v-if="messages.length === 0 && !isRunning" class="chat-welcome">
      <div class="chat-welcome-mark">审</div>
      <h2 class="chat-welcome-title">{{ MESSAGES.WELCOME_TITLE }}</h2>
      <p class="chat-welcome-desc">{{ MESSAGES.WELCOME_DESC }}</p>
      <div class="chat-welcome-examples">
        <button
          v-for="example in examples"
          :key="example.task"
          class="chat-welcome-example"
          type="button"
          @click="$emit('quick-task', example.task)"
        >
          {{ example.label }}
        </button>
      </div>
    </div>

    <div v-else-if="messages.length === 0 && isRunning" class="chat-loading">
      <div class="chat-loading-dot"></div>
      <p>{{ MESSAGES.LOADING_CONNECTING }}</p>
    </div>

    <template v-else>
      <ChatMessage
        v-for="(item, index) in messages"
        :key="item.id"
        :item="item"
        :is-streaming="isRunning && index === messages.length - 1"
        @preview="$emit('preview', $event)"
      />
    </template>
  </div>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, watch, nextTick } from "vue";
import type { ChatMessageItem } from "../../types/chat";
import { MESSAGES } from "../../constants/messages";
import { useAutoScroll } from "../../composables/useAutoScroll";
import ChatMessage from "./ChatMessage.vue";

interface Props {
  messages: ChatMessageItem[];
  isRunning: boolean;
}

const props = defineProps<Props>();
const emit = defineEmits<{
  "quick-task": [task: string];
  preview: [file: Extract<ChatMessageItem, { type: "file" }>];
}>();

const examples = [
  { label: MESSAGES.QUICK_FORMAT, task: "审核这份通知的格式规范" },
  { label: MESSAGES.QUICK_CONTENT, task: "检查公文内容是否符合规范要求" },
  { label: MESSAGES.QUICK_FULL, task: "全面审核这份文档" },
];

const { containerRef, mount, unmount, forceScrollToBottom, scrollToBottom } =
  useAutoScroll();

onMounted(() => mount());
onUnmounted(() => unmount());

watch(
  () => props.messages.length,
  () => nextTick(forceScrollToBottom),
);

watch(
  () => props.isRunning,
  () => nextTick(scrollToBottom),
);
</script>

<style scoped>
.chat-area {
  flex: 1;
  overflow-y: auto;
  padding: 24px 16px;
  display: flex;
  flex-direction: column;
  gap: 16px;
  background: var(--chat-bg-body);
}

.chat-welcome,
.chat-loading {
  margin: auto;
  display: flex;
  flex-direction: column;
  align-items: center;
  text-align: center;
  gap: 14px;
  padding: 48px 24px;
}

.chat-welcome-mark {
  width: 64px;
  height: 64px;
  border: 2px solid var(--chat-accent);
  border-radius: var(--chat-radius-md);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 28px;
  font-weight: 700;
  color: var(--chat-accent);
}

.chat-welcome-title {
  font-size: 22px;
  font-weight: 600;
  color: var(--chat-text-primary);
  margin: 0;
}

.chat-welcome-desc {
  font-size: 15px;
  color: var(--chat-text-secondary);
  margin: 0;
}

.chat-welcome-examples {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  justify-content: center;
  margin-top: 8px;
}

.chat-welcome-example {
  padding: 8px 16px;
  border: 1px solid var(--chat-border);
  border-radius: var(--chat-radius-md);
  background: var(--chat-bg-card);
  color: var(--chat-text-secondary);
  font-size: 14px;
  cursor: pointer;
  transition:
    border-color 0.15s,
    color 0.15s;
}

.chat-welcome-example:hover {
  border-color: var(--chat-accent);
  color: var(--chat-accent);
}

.chat-loading-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: var(--chat-accent);
  animation: pulse 1.2s ease-in-out infinite;
}

@keyframes pulse {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.3;
  }
}
</style>
```

- [ ] **Step 3: Commit**

```bash
git add src/components/chat/ChatMessage.vue src/components/chat/ChatArea.vue
git commit -m "feat(chat): add chat area and message dispatcher"
```

---

### Task 11: Create the input area

**Files:**
- Create: `src/components/chat/InputArea.vue`

- [ ] **Step 1: Implement the component**

```vue
<template>
  <div class="input-area">
    <div v-if="error || fileError" class="input-area-error">
      {{ error || fileError }}
    </div>
    <div v-if="fileName" class="input-area-file">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
        <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
        <polyline points="14 2 14 8 20 8" />
      </svg>
      <span>{{ fileName }}</span>
      <button class="input-area-file-remove" type="button" @click="clearFile">×</button>
    </div>

    <div class="input-area-bar">
      <input
        ref="fileInput"
        type="file"
        accept=".pdf,.docx,.bmp,.jpg,.jpeg,.png,.gif,.tif,.tiff"
        style="display: none"
        @change="handleFileChange"
      />
      <button
        class="input-area-attach"
        type="button"
        :title="MESSAGES.CHAT_ATTACH"
        @click="fileInput?.click()"
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <line x1="12" y1="5" x2="12" y2="19" />
          <line x1="5" y1="12" x2="19" y2="12" />
        </svg>
      </button>
      <textarea
        ref="textareaRef"
        v-model="task"
        class="input-area-textarea"
        :placeholder="MESSAGES.CHAT_PLACEHOLDER"
        rows="1"
        @input="autoResize"
        @keydown.enter.prevent="handleEnter"
      />
      <span v-if="modelName" class="input-area-model">{{ MESSAGES.CHAT_MODEL }}: {{ modelName }}</span>
      <button
        class="input-area-send"
        type="button"
        :disabled="!isRunning && (!canSubmit || uploading)"
        @click="handleClick"
      >
        <svg v-if="isRunning" viewBox="0 0 24 24" fill="currentColor">
          <rect x="4" y="4" width="16" height="16" rx="2" />
        </svg>
        <svg v-else-if="!uploading" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <line x1="22" y1="2" x2="11" y2="13" />
          <polygon points="22 2 15 22 11 13 2 9 22 2" />
        </svg>
        <span v-else class="input-area-spinner"></span>
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, nextTick } from "vue";
import { MESSAGES } from "../../constants/messages";

interface Props {
  modelName?: string;
  uploading?: boolean;
  error?: string;
  isRunning?: boolean;
}

const props = defineProps<Props>();
const emit = defineEmits<{
  submit: [task: string, file?: File];
  stop: [];
}>();

const task = ref("");
const fileError = ref("");
const fileName = ref("");
const selectedFile = ref<File | null>(null);
const fileInput = ref<HTMLInputElement | null>(null);
const textareaRef = ref<HTMLTextAreaElement | null>(null);

const canSubmit = computed(() => task.value.trim().length > 0);
const MAX_FILE_SIZE = 50 * 1024 * 1024;

function autoResize() {
  const el = textareaRef.value;
  if (!el) return;
  el.style.height = "auto";
  el.style.height = Math.min(el.scrollHeight, 120) + "px";
}

function handleEnter(event: KeyboardEvent) {
  if (event.shiftKey) return;
  handleClick();
}

function handleClick() {
  if (props.isRunning) {
    emit("stop");
    return;
  }
  if (!canSubmit.value) return;
  emit("submit", task.value.trim(), selectedFile.value || undefined);
  task.value = "";
  clearFile();
  nextTick(() => {
    const el = textareaRef.value;
    if (el) el.style.height = "auto";
  });
}

function handleFileChange(event: Event) {
  const target = event.target as HTMLInputElement;
  const file = target.files?.[0];
  if (!file) return;
  if (file.size > MAX_FILE_SIZE) {
    fileError.value = MESSAGES.FILE_TOO_LARGE((file.size / 1024 / 1024).toFixed(1));
    target.value = "";
    return;
  }
  fileName.value = file.name;
  selectedFile.value = file;
  fileError.value = "";
  target.value = "";
}

function clearFile() {
  fileName.value = "";
  selectedFile.value = null;
  fileError.value = "";
}
</script>

<style scoped>
.input-area {
  flex-shrink: 0;
  background: var(--chat-bg-card);
  border-top: 1px solid var(--chat-border);
  padding: 12px 16px;
}

.input-area-error {
  margin-bottom: 8px;
  padding: 8px 12px;
  border-radius: var(--chat-radius-md);
  background: rgba(239, 68, 68, 0.1);
  color: #ef4444;
  font-size: 14px;
}

.input-area-file {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 8px;
  padding: 6px 10px;
  border: 1px solid var(--chat-border);
  border-radius: var(--chat-radius-md);
  background: var(--chat-bg-hover);
  font-size: 14px;
  color: var(--chat-text-secondary);
}

.input-area-file-remove {
  border: none;
  background: transparent;
  color: var(--chat-text-tertiary);
  font-size: 18px;
  line-height: 1;
  cursor: pointer;
  padding: 0 2px;
}

.input-area-file-remove:hover {
  color: #ef4444;
}

.input-area-bar {
  display: flex;
  align-items: flex-end;
  gap: 10px;
  border: 1px solid var(--chat-border);
  border-radius: var(--chat-radius-lg);
  padding: 10px 12px;
  background: var(--chat-bg-card);
}

.input-area-attach,
.input-area-send {
  width: 36px;
  height: 36px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: none;
  border-radius: var(--chat-radius-md);
  cursor: pointer;
  flex-shrink: 0;
}

.input-area-attach {
  background: transparent;
  color: var(--chat-text-secondary);
}

.input-area-attach:hover {
  background: var(--chat-bg-hover);
  color: var(--chat-text-primary);
}

.input-area-attach svg {
  width: 20px;
  height: 20px;
}

.input-area-textarea {
  flex: 1;
  border: none;
  background: transparent;
  resize: none;
  outline: none;
  font-size: 15px;
  line-height: 1.5;
  color: var(--chat-text-primary);
  min-height: 24px;
  max-height: 120px;
  padding: 6px 4px;
}

.input-area-textarea::placeholder {
  color: var(--chat-text-tertiary);
}

.input-area-model {
  font-size: 12px;
  color: var(--chat-text-tertiary);
  padding: 8px 4px;
  flex-shrink: 0;
  display: none;
}

@media (min-width: 640px) {
  .input-area-model {
    display: inline;
  }
}

.input-area-send {
  background: var(--chat-accent);
  color: #fff;
}

.input-area-send:hover:not(:disabled) {
  background: var(--chat-accent-hover);
}

.input-area-send:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.input-area-send svg {
  width: 18px;
  height: 18px;
}

.input-area-spinner {
  width: 16px;
  height: 16px;
  border: 2px solid rgba(255, 255, 255, 0.3);
  border-top-color: #fff;
  border-radius: 50%;
  animation: spin 0.6s linear infinite;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}
</style>
```

- [ ] **Step 2: Commit**

```bash
git add src/components/chat/InputArea.vue
git commit -m "feat(chat): add bottom input area"
```

---

### Task 12: Create the file preview drawer

**Files:**
- Create: `src/components/chat/FilePreviewDrawer.vue`

- [ ] **Step 1: Implement the component**

```vue
<template>
  <Teleport to="body">
    <Transition name="drawer-fade">
      <div v-if="isOpen" class="file-drawer-backdrop" @click="$emit('close')">
        <Transition name="drawer-slide">
          <div
            v-if="isOpen"
            class="file-drawer"
            @click.stop
            role="dialog"
            aria-modal="true"
          >
            <div class="file-drawer-header">
              <h3 class="file-drawer-title" :title="file?.name">{{ file?.name }}</h3>
              <div class="file-drawer-actions">
                <a
                  v-if="file?.url"
                  class="file-drawer-action"
                  :href="file.url"
                  :download="file.name"
                >
                  {{ MESSAGES.CHAT_PREVIEW_DOWNLOAD }}
                </a>
                <button
                  class="file-drawer-close"
                  type="button"
                  @click="$emit('close')"
                >
                  ×
                </button>
              </div>
            </div>

            <div class="file-drawer-body">
              <img
                v-if="file && isImage(file.mimeType)"
                :src="file.url"
                :alt="file.name"
                class="file-drawer-image"
              />
              <iframe
                v-else-if="file && isPdf(file.mimeType)"
                :src="file.url"
                class="file-drawer-frame"
                :title="file.name"
              ></iframe>
              <div v-else class="file-drawer-unsupported">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="48" height="48">
                  <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
                  <polyline points="14 2 14 8 20 8" />
                </svg>
                <p>{{ MESSAGES.CHAT_PREVIEW_UNSUPPORTED }}</p>
                <a v-if="file?.url" class="file-drawer-download-link" :href="file.url" :download="file.name">
                  {{ MESSAGES.CHAT_PREVIEW_DOWNLOAD }}
                </a>
              </div>
            </div>
          </div>
        </Transition>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, watch } from "vue";
import type { ChatFileItem } from "../../types/chat";
import { MESSAGES } from "../../constants/messages";

interface Props {
  isOpen: boolean;
  file: ChatFileItem | null;
}

const props = defineProps<Props>();
const emit = defineEmits<{ close: [] }>();

function isImage(mime: string): boolean {
  return mime.startsWith("image/");
}

function isPdf(mime: string): boolean {
  return mime === "application/pdf";
}

function onKeydown(event: KeyboardEvent) {
  if (event.key === "Escape" && props.isOpen) {
    emit("close");
  }
}

onMounted(() => window.addEventListener("keydown", onKeydown));
onUnmounted(() => window.removeEventListener("keydown", onKeydown));

watch(
  () => props.isOpen,
  (open) => {
    document.body.style.overflow = open ? "hidden" : "";
  },
);
</script>

<style scoped>
.file-drawer-backdrop {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.4);
  z-index: 200;
  display: flex;
  justify-content: flex-end;
}

.file-drawer {
  width: var(--chat-drawer-width);
  max-width: 100%;
  height: 100%;
  background: var(--chat-bg-card);
  display: flex;
  flex-direction: column;
  box-shadow: -4px 0 24px rgba(0, 0, 0, 0.12);
}

.file-drawer-header {
  flex-shrink: 0;
  height: 56px;
  padding: 0 16px;
  border-bottom: 1px solid var(--chat-border);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.file-drawer-title {
  flex: 1;
  font-size: 15px;
  font-weight: 600;
  color: var(--chat-text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  margin: 0;
}

.file-drawer-actions {
  display: flex;
  align-items: center;
  gap: 12px;
}

.file-drawer-action {
  font-size: 14px;
  color: var(--chat-accent);
  text-decoration: none;
}

.file-drawer-action:hover {
  text-decoration: underline;
}

.file-drawer-close {
  width: 32px;
  height: 32px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: none;
  background: transparent;
  color: var(--chat-text-secondary);
  font-size: 24px;
  line-height: 1;
  cursor: pointer;
  border-radius: var(--chat-radius-md);
}

.file-drawer-close:hover {
  background: var(--chat-bg-hover);
  color: var(--chat-text-primary);
}

.file-drawer-body {
  flex: 1;
  overflow: auto;
  padding: 16px;
  display: flex;
  align-items: center;
  justify-content: center;
}

.file-drawer-image {
  max-width: 100%;
  max-height: 100%;
  object-fit: contain;
  border-radius: var(--chat-radius-md);
}

.file-drawer-frame {
  width: 100%;
  height: 100%;
  border: none;
  border-radius: var(--chat-radius-md);
}

.file-drawer-unsupported {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
  text-align: center;
  color: var(--chat-text-secondary);
}

.file-drawer-download-link {
  padding: 8px 16px;
  border-radius: var(--chat-radius-md);
  background: var(--chat-accent);
  color: #fff;
  text-decoration: none;
  font-size: 14px;
}

.drawer-fade-enter-active,
.drawer-fade-leave-active {
  transition: opacity 0.25s ease;
}

.drawer-fade-enter-from,
.drawer-fade-leave-to {
  opacity: 0;
}

.drawer-slide-enter-active,
.drawer-slide-leave-active {
  transition: transform 0.25s ease;
}

.drawer-slide-enter-from,
.drawer-slide-leave-to {
  transform: translateX(100%);
}

@media (max-width: 768px) {
  .file-drawer {
    width: 100%;
  }
}
</style>
```

- [ ] **Step 2: Commit**

```bash
git add src/components/chat/FilePreviewDrawer.vue
git commit -m "feat(chat): add file preview drawer"
```

---

### Task 13: Assemble the chat layout

**Files:**
- Create: `src/components/chat/ChatLayout.vue`

- [ ] **Step 1: Implement the layout shell**

```vue
<template>
  <div class="chat-layout">
    <ChatSidebar
      :sessions="sessions"
      :loading="historyLoading"
      :is-open="sidebarOpen"
      :user-role="userRole"
      @new-session="$emit('new-session')"
      @select="$emit('history-select', $event)"
      @delete="$emit('history-delete', $event)"
      @logout="$emit('logout')"
    />
    <div class="chat-layout-main">
      <ChatHeader
        :title="title"
        :model-name="modelName"
        :username="username"
        :user-role="userRole"
        @toggle-sidebar="$emit('toggle-sidebar')"
        @toggle-theme="$emit('toggle-theme')"
        @logout="$emit('logout')"
      />
      <ChatArea
        :messages="messages"
        :is-running="isRunning"
        @quick-task="$emit('quick-task', $event)"
        @preview="$emit('preview-file', $event)"
      />
      <InputArea
        :model-name="modelName"
        :uploading="uploading"
        :error="uploadError"
        :is-running="isRunning"
        @submit="(task, file) => $emit('submit', task, file)"
        @stop="$emit('stop')"
      />
    </div>
    <FilePreviewDrawer
      :is-open="drawerOpen"
      :file="currentFile"
      @close="$emit('close-preview')"
    />
  </div>
</template>

<script setup lang="ts">
import type { SessionSummary } from "../../types/agent";
import type { ChatFileItem, ChatMessageItem } from "../../types/chat";
import ChatSidebar from "./ChatSidebar.vue";
import ChatHeader from "./ChatHeader.vue";
import ChatArea from "./ChatArea.vue";
import InputArea from "./InputArea.vue";
import FilePreviewDrawer from "./FilePreviewDrawer.vue";

interface Props {
  title: string;
  modelName: string;
  messages: ChatMessageItem[];
  sessions: SessionSummary[];
  historyLoading: boolean;
  isRunning: boolean;
  uploading: boolean;
  uploadError: string;
  drawerOpen: boolean;
  currentFile: ChatFileItem | null;
  sidebarOpen: boolean;
  username?: string;
  userRole?: string;
}

defineProps<Props>();
defineEmits<{
  "new-session": [];
  "history-select": [id: string];
  "history-delete": [id: string];
  logout: [];
  "toggle-sidebar": [];
  "toggle-theme": [];
  "quick-task": [task: string];
  "preview-file": [file: ChatFileItem];
  "close-preview": [];
  submit: [task: string, file?: File];
  stop: [];
}>();
</script>

<style scoped>
.chat-layout {
  display: flex;
  height: 100vh;
  overflow: hidden;
  background: var(--chat-bg-body);
}

.chat-layout-main {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
}
</style>
```

- [ ] **Step 2: Commit**

```bash
git add src/components/chat/ChatLayout.vue
git commit -m "feat(chat): add chat layout shell"
```

---

### Task 14: Rewrite HomeView to wire everything together

**Files:**
- Modify: `src/views/HomeView.vue`

- [ ] **Step 1: Replace the view content**

```vue
<template>
  <ChatLayout
    :title="title"
    :model-name="session.modelName"
    :messages="messages"
    :sessions="historySessions"
    :history-loading="historyLoading"
    :is-running="isRunning"
    :uploading="uploading"
    :upload-error="uploadError"
    :drawer-open="filePreview.isOpen.value"
    :current-file="filePreview.currentFile.value"
    :sidebar-open="sidebarOpen"
    :username="user?.username"
    :user-role="user?.role"
    @new-session="newSessionWithCleanup"
    @history-select="handleHistorySelect"
    @history-delete="handleHistoryDelete"
    @logout="handleLogout"
    @toggle-sidebar="sidebarOpen = !sidebarOpen"
    @toggle-theme="toggleTheme"
    @quick-task="(task: string) => handleSubmit(task)"
    @preview-file="filePreview.open"
    @close-preview="filePreview.close"
    @submit="handleSubmit"
    @stop="stop"
  />
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted } from "vue";
import { useRouter } from "vue-router";
import { useAgentSession } from "../composables/useAgentSession";
import { useHistory } from "../composables/useHistory";
import { useAuth } from "../composables/useAuth";
import { useChatMessages } from "../composables/useChatMessages";
import { useFilePreview } from "../composables/useFilePreview";
import { api } from "../api/client";
import type { ChatFileRecord } from "../types/chat";
import ChatLayout from "../components/chat/ChatLayout.vue";

const router = useRouter();
const { user, logout } = useAuth();
const { session, connect, newSession, stop, isRunning } = useAgentSession();
const {
  sessions: historySessions,
  loading: historyLoading,
  fetchSessions,
  loadSession,
  deleteSession,
} = useHistory();
const filePreview = useFilePreview();

const sidebarOpen = ref(false);
const uploading = ref(false);
const uploadError = ref("");
const uploadedFiles = ref<ChatFileRecord[]>([]);

const { messages, title } = useChatMessages(session, uploadedFiles);

function revokeUploadedFiles() {
  for (const f of uploadedFiles.value) {
    URL.revokeObjectURL(f.url);
  }
  uploadedFiles.value = [];
}

function newSessionWithCleanup() {
  revokeUploadedFiles();
  newSession();
}

async function handleSubmit(task: string, file?: File) {
  let fileId: string | undefined;
  let fileUrl: string | undefined;

  if (file) {
    uploading.value = true;
    uploadError.value = "";
    fileUrl = URL.createObjectURL(file);
    try {
      const result = await api.uploadFile(file);
      fileId = result.fileId;
      uploadedFiles.value = [
        ...uploadedFiles.value,
        { fileId, name: file.name, url: fileUrl },
      ];
    } catch (e: unknown) {
      uploadError.value = e instanceof Error ? e.message : "上传失败";
      if (fileUrl) URL.revokeObjectURL(fileUrl);
      uploading.value = false;
      return;
    } finally {
      uploading.value = false;
    }
  }

  connect(task, fileId, file?.name);
}

async function handleHistoryDelete(id: string) {
  const previous = historySessions.value;
  historySessions.value = historySessions.value.filter((s) => s.id !== id);
  try {
    await deleteSession(id);
  } catch (_err) {
    historySessions.value = previous;
    console.warn("删除会话失败", _err);
  }
}

async function handleHistorySelect(id: string) {
  const loaded = await loadSession(id);
  if (loaded) {
    revokeUploadedFiles();
    Object.assign(session, loaded);
    sidebarOpen.value = false;
  }
}

async function handleLogout() {
  await logout();
  router.push("/login");
}

function toggleTheme() {
  const html = document.documentElement;
  const current = html.getAttribute("data-theme") || "light";
  html.setAttribute("data-theme", current === "light" ? "dark" : "light");
}

onMounted(() => {
  fetchSessions();
  if (!document.documentElement.hasAttribute("data-theme")) {
    document.documentElement.setAttribute("data-theme", "light");
  }
});

onUnmounted(() => {
  revokeUploadedFiles();
});
</script>
```

- [ ] **Step 2: Verify the single composable usage**

The script should expose only one `useChatMessages` call that receives the `uploadedFiles` ref:

```typescript
const { messages, title } = useChatMessages(session, uploadedFiles);
```

No second pair of `messages`/`title` should exist. This works because `useChatMessages` accepts a `Ref<ChatFileRecord[]>` and unwraps it inside the `computed`.

- [ ] **Step 3: Commit**

```bash
git add src/views/HomeView.vue
git commit -m "feat(chat): wire new chat layout into HomeView"
```

---

### Task 15: Update test script entrypoint

**Files:**
- Modify: `package.json:9`

- [ ] **Step 1: Add the new test to the `test` command**

Append `node scripts/test-chat-messages.mjs` to the `test` script:

```json
"test": "node scripts/test-tool-calls.mjs && node scripts/test-subagent-tree.mjs && node scripts/test-session-event-handlers.mjs && node scripts/test-streaming-markdown.mjs && node scripts/test-tool-progress.mjs && node scripts/test-session-utils.mjs && node scripts/test-step-sync.mjs && node scripts/test-tool-result-status.mjs && node scripts/test-chat-messages.mjs",
```

- [ ] **Step 2: Run tests**

```bash
npm test
```

Expected: all scripts pass, ending with `chatMessages verification passed`.

- [ ] **Step 3: Commit**

```bash
git add package.json
git commit -m "chore(chat): include chat message tests in npm test"
```

---

### Task 16: Build and type-check

**Files:**
- All modified `.vue` and `.ts` files

- [ ] **Step 1: Run the production build**

```bash
cd /home/lmwl/Documents/docaudit/agent/courtier/webui
npm run build
```

Expected: `vue-tsc` passes and Vite emits the bundle into `dist/` without errors.

- [ ] **Step 2: Fix any TS or lint issues**

Common fixes:
- Import paths: all new imports use `../..` relative to `src/components/chat/`.
- `Extract<ChatMessageItem, { type: "file" }>` must resolve to `ChatFileItem`; ensure `ChatMessageItem` union includes `ChatFileItem`.
- `useChatMessages` accepts a `Ref<ChatFileRecord[]>`; pass the ref from `HomeView`, not `ref.value`.

- [ ] **Step 3: Commit**

```bash
git add -A
git commit -m "fix(chat): resolve build/type errors"
```

---

## Self-Review

**1. Spec coverage:**
- Light theme default → Task 5 sets `data-theme="light"` in `HomeView`.
- Left sidebar with real functions → Task 7.
- Conversation summary header → Task 8 + `deriveConversationTitle` in Task 3.
- Collapsible thinking cards → Task 9 (ThinkingCard).
- File cards → Task 9 (FileCard).
- Right-side preview drawer → Task 12.
- Data flow reuses `useAgentSession` → Task 14.
- Tests → Task 3 script + Task 15 wiring.

**2. Placeholder scan:**
- No TBD/TODO.
- Every task includes concrete code, exact commands, and expected output.
- Error/stopped status messages are implemented.

**3. Type consistency:**
- `Message.fileId` added in Task 1 and used in Task 2.
- `ChatFileRecord` shape used consistently across `chatMessages.ts`, `useChatMessages.ts`, and `HomeView.vue`.
- `ChatMessageItem` union includes all rendered types.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-15-chat-ui-redesign-plan.md`.

Two execution options:

1. **Subagent-Driven (recommended)** — dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** — execute tasks in this session using `superpowers:executing-plans`, batch execution with checkpoints.

Which approach?
