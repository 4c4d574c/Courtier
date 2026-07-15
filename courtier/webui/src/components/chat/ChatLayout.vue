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
