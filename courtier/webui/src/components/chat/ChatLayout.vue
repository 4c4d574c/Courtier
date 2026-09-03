<template>
  <div class="chat-layout">
    <ChatSidebar
      :sessions="sessions"
      :loading="historyLoading"
      :list-error="listError"
      :is-open="sidebarOpen"
      :user-role="userRole"
      :username="username"
      @new-session="$emit('new-session')"
      @select="$emit('history-select', $event)"
      @delete="$emit('history-delete', $event)"
      @rename="$emit('history-rename', $event.id, $event.task)"
      @pin="$emit('history-pin', $event.id, $event.pinned)"
      @logout="$emit('logout')"
    />
    <div class="chat-layout-main">
      <ChatHeader
        :title="title"
        :model-events="session.modelEvents"
        @toggle-sidebar="$emit('toggle-sidebar')"
      />
      <div class="chat-layout-chat">
        <ChatArea
          :messages="messages"
          :is-running="isRunning"
          :queue-position="session.queuePosition"
          :can-edit="canEdit"
          :edit-hint="editHint"
          @preview="$emit('preview-file', $event)"
          @citation-click="$emit('citation-click', $event)"
          @edit-submit="$emit('edit-submit', $event)"
        />
        <div v-if="session.refusalNotice" class="refusal-banner">
          {{ session.refusalNotice }}
        </div>
        <InputArea
          :model-name="session.modelName"
          :uploading="uploading"
          :error="uploadError"
          :is-running="isRunning"
          :pending-confirmations="session.pendingConfirmations ?? []"
          @resolve="(id, decision) => $emit('resolve-confirmation', id, decision)"
          @submit="(task, docFile, mediaFiles) => $emit('submit', task, docFile, mediaFiles)"
          @stop="$emit('stop')"
        />
      </div>
    </div>
    <FilePreviewPanel
      :is-open="drawerOpen"
      :file="currentFile"
      :citation="currentCitation"
      @close="$emit('close-preview')"
    />
  </div>
</template>

<script setup lang="ts">
import type { CitationHit, Session, SessionSummary } from "../../types/agent";
import type { ChatFileItem, ChatMessageItem } from "../../types/chat";
import ChatSidebar from "./ChatSidebar.vue";
import ChatHeader from "./ChatHeader.vue";
import ChatArea from "./ChatArea.vue";
import InputArea from "./InputArea.vue";
import FilePreviewPanel from "./FilePreviewPanel.vue";

interface Props {
  title: string;
  session: Session;
  messages: ChatMessageItem[];
  sessions: SessionSummary[];
  historyLoading: boolean;
  /** Non-empty = last sidebar-list refresh failed; sidebar keeps old data. */
  listError?: string | null;
  isRunning: boolean;
  uploading: boolean;
  uploadError: string;
  drawerOpen: boolean;
  currentFile: ChatFileItem | null;
  currentCitation: CitationHit | null;
  sidebarOpen: boolean;
  username?: string;
  userRole?: string;
  /** User-bubble edit entry: false hides it (running session). */
  canEdit?: boolean;
  /** Non-empty disables editing with the reason shown as tooltip. */
  editHint?: string;
}

defineProps<Props>();
defineEmits<{
  "new-session": [];
  "history-select": [id: string];
  "history-delete": [id: string];
  "history-rename": [id: string, task: string];
  "history-pin": [id: string, pinned: boolean];
  logout: [];
  "toggle-sidebar": [];
  "preview-file": [file: ChatFileItem];
  "citation-click": [hit: CitationHit | undefined];
  "close-preview": [];
  submit: [task: string, document?: File, media?: File[]];
  stop: [];
  "resolve-confirmation": [
    confirmationId: string,
    decision: "approve" | "approve_session" | "deny",
  ];
  "edit-submit": [payload: { turnIndex: number; text: string }];
}>();
</script>

<style scoped>
.chat-layout {
  display: flex;
  height: 100vh;
  overflow: hidden;
  background: var(--chat-bg-body);
  position: relative;
}

.chat-layout-main {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
}

/* Messages + input live in one column so the input stays aligned with the
   chat when the preview panel docks beside them. The panel is a third flex
   child of the layout, spanning the full height — header and chat narrow
   together to make room for it. */
.chat-layout-chat {
  flex: 1;
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
}
.refusal-banner {
  margin: 8px 0;
  padding: 10px 14px;
  border-radius: var(--chat-radius-md);
  border-left: 3px solid #ef4444;
  background: var(--chat-bg-card);
  font-size: 14px;
  color: var(--chat-text-secondary, inherit);
}
</style>
