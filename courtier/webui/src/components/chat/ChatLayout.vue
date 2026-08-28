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
      @rename="$emit('history-rename', $event.id, $event.task)"
      @pin="$emit('history-pin', $event.id, $event.pinned)"
      @logout="$emit('logout')"
    />
    <div class="chat-layout-main">
      <ChatHeader
        :title="title"
        :username="username"
        :user-role="userRole"
        :model-events="session.modelEvents"
        @toggle-sidebar="$emit('toggle-sidebar')"
        @toggle-theme="$emit('toggle-theme')"
        @logout="$emit('logout')"
      />
      <div class="chat-layout-body">
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
          <InputArea
            :model-name="session.modelName"
            :uploading="uploading"
            :error="uploadError"
            :is-running="isRunning"
            @submit="(task, file) => $emit('submit', task, file)"
            @stop="$emit('stop')"
          />
        </div>
        <FilePreviewPanel
          :is-open="drawerOpen"
          :file="currentFile"
          :citation="currentCitation"
          @close="$emit('close-preview')"
        />
      </div>
    </div>
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
  "toggle-theme": [];
  "preview-file": [file: ChatFileItem];
  "citation-click": [hit: CitationHit | undefined];
  "close-preview": [];
  submit: [task: string, file?: File];
  stop: [];
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
   chat when the preview panel docks beside them. */
.chat-layout-body {
  flex: 1;
  display: flex;
  min-height: 0;
}

.chat-layout-chat {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}
</style>
