# Courtier WebUI Chat Redesign — Design Spec

> **Date:** 2026-07-15  
> **Scope:** Chat interface + left navigation overall layout  
> **Approach:** Complete rewrite of chat components (方案 2)

---

## 1. Goal

Redesign the Courtier webui frontend to match a Kimi-style chat interface: light theme by default, collapsible thinking cards, file cards in the chat stream, a right-side preview drawer, a left navigation sidebar, and a conversation-summary header.

---

## 2. Overall Layout

```
┌─────────────────┬─────────────────────────────────────────────┐
│  Logo           │  Header: [会话摘要标题]        [用户菜单]     │
│  新建会话        ├─────────────────────────────────────────────┤
│  历史会话        │                                             │
│  ──────────────  │           聊天消息流                         │
│  管理入口（admin）│                                             │
│                 │  [文件卡片]                                   │
│                 │  [用户气泡]                                   │
│                 │  [AI 思考折叠卡片]                            │
│                 │  [AI 回复内容]                                │
│                 │                                             │
│                 ├─────────────────────────────────────────────┤
│                 │  [附件] [输入框]              [模型] [发送]   │
└─────────────────┴─────────────────────────────────────────────┘
```

- **Left sidebar:** fixed width 260 px, collapsible on mobile to an icon rail.
- **Right main area:** header, chat area, input area stacked vertically.
- **File preview:** slides in from the right as a drawer (480 px, full-screen on mobile).

---

## 3. Component Structure

All new chat components live under `webui/src/components/chat/`.

| Component | Responsibility |
|-----------|----------------|
| `ChatLayout.vue` | Top-level layout: sidebar + main area. |
| `ChatSidebar.vue` | Logo, new-session button, history list, admin entries, user area. |
| `ChatHeader.vue` | Conversation summary title, theme toggle, user menu. |
| `ChatArea.vue` | Scrollable message stream and auto-scroll behavior. |
| `ChatMessage.vue` | Message shell that delegates to the concrete sub-component. |
| `UserMessage.vue` | User message bubble. |
| `AssistantMessage.vue` | Assistant reply content. |
| `ThinkingCard.vue` | Collapsible thinking process card. |
| `FileCard.vue` | Uploaded/result file card; emits `preview-file` on click. |
| `InputArea.vue` | Attachment button, textarea, model display, send button. |
| `FilePreviewDrawer.vue` | Right-side drawer for file preview. |

Legacy components (`Terminal.vue`, `MainPanel.vue`, `InputPanel.vue`, `HistoryPanel.vue`, `StepGroup.vue`) remain in the repository until the new chat page is stable. `HomeView.vue` mounts `ChatLayout` as its root child and passes the session/history state down through props and composables.

---

## 4. Theme System

- Default theme is **light**.
- Theme attribute `data-theme="light" | "dark"` is set on `<html>`.
- Design tokens are defined as CSS custom properties in `src/styles/theme.css`, imported from `src/main.ts`.
- Key palette (light mode):

```css
:root {
  --bg-body: #f4f5f7;
  --bg-card: #ffffff;
  --text-primary: #111827;
  --text-secondary: #6b7280;
  --accent: #4f46e5;
  --accent-hover: #4338ca;
  --border: #e5e7eb;
  --shadow: 0 1px 3px rgba(0, 0, 0, 0.08);
}
```

- Font stack stays system default to avoid extra asset loading:
  `-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "PingFang SC", "Microsoft YaHei"`.
- Border radius tokens: `8px` for inputs/buttons, `12px` for cards/bubbles.

---

## 5. Data Flow

- `useAgentSession` continues to own the SSE connection, session state, and message sending.
- New `useChatMessages` composable normalizes raw SSE events into a render-friendly list:

```ts
type ChatMessageItem =
  | { type: 'user'; id: string; content: string }
  | { type: 'assistant'; id: string; content: string }
  | { type: 'thinking'; id: string; content: string; isOpen: boolean }
  | { type: 'file'; id: string; name: string; url: string; mimeType: string };
```

- `think`, `act`, `observe` events are grouped into `thinking` cards.
- File upload events become `file` cards.
- New `useFilePreview` composable manages the right drawer:
  - `openPreview(file)`
  - `closePreview()`
  - reactive `currentFile` and `isOpen`
- Conversation summary title:
  - Use `session.title` when available from the backend.
  - Fall back to the first user message truncated to 20 characters.

---

## 6. File Preview Drawer

- Width: 480 px on desktop, full-screen on mobile.
- Triggered by clicking any `FileCard`.
- Rendering strategy by MIME type:
  - **Image:** `<img>` with object-fit contain.
  - **PDF:** `<iframe src="...">` for native browser rendering.
  - **DOCX / Office / other:** show filename, size, and a download button; inform the user that the browser cannot preview this file directly.
- Close via mask click, close button, or `Escape` key.
- Drawer state is decoupled from `ChatArea` and kept in `useFilePreview`.

---

## 7. Sidebar Content

Sidebar shows only currently real functions:

- Logo / product name
- **New session** button
- **History** list (recent sessions)
- Admin-only entries when the user has admin role
- User profile / settings entry at the bottom

No speculative menu items (billing, teams, etc.) are added.

---

## 8. Model Selector

- Display the currently active model name in the input area.
- The UI reserves switching for a later iteration; this redesign only shows the label.

---

## 9. Testing Plan

### Visual regression
- Screenshot key breakpoints: 320, 768, 1024, 1440.
- Cover: empty state, populated chat stream, drawer open.

### Accessibility
- Automated a11y checks.
- Keyboard navigation through sidebar, messages, and drawer.
- Reduced-motion behavior.
- Color contrast verification.

### Responsive
- Mobile sidebar collapse.
- Drawer full-screen behavior.
- Input area avoids soft-keyboard occlusion.

### Unit tests
- `useChatMessages` event normalization.
- `useFilePreview` open/close state transitions.
- Title truncation helper.

### E2E
- New session → type message → send → receive assistant reply.
- Mock SSE in Playwright for deterministic assertions.

---

## 10. Out of Scope

- Model switching UI (display only).
- Multi-language UI strings (kept in existing locale handling).
- Backend API changes; only frontend components are affected.
- Real-time collaboration or shared sessions.

---

## 11. Success Criteria

- The chat page renders with the new layout and light theme by default.
- Users can start a new session, send text, and receive streamed assistant replies.
- Thinking process is shown in a collapsible card.
- Uploaded/result files appear as cards and open the right-side preview drawer on click.
- The header displays a meaningful conversation summary title.
- All existing backend integration continues to work without API changes.
- Unit-test coverage for new composables is ≥ 80%.
