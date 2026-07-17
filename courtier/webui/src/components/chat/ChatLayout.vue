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
        :model-name="session.modelName"
        :username="username"
        :user-role="userRole"
        :model-events="session.modelEvents"
        @toggle-sidebar="$emit('toggle-sidebar')"
        @toggle-debug="debugOpen = !debugOpen"
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
        :model-name="session.modelName"
        :uploading="uploading"
        :error="uploadError"
        :is-running="isRunning"
        @submit="(task, file) => $emit('submit', task, file)"
        @stop="$emit('stop')"
      />
    </div>
    <DebugPanel
      :open="debugOpen"
      :model-events="session.modelEvents"
      :hint-events="session.hintEvents"
      :loop-completed="session.loopCompleted"
      :tree-json="session.treeJson"
      :current-node-id="session.currentNodeId"
      @close="debugOpen = false"
      @fork="$emit('fork-session', $event)"
      @rewind="$emit('rewind-session', $event)"
    />
    <FilePreviewDrawer
      :is-open="drawerOpen"
      :file="currentFile"
      @close="$emit('close-preview')"
    />
  </div>
</template>

<script setup lang="ts">
import { ref } from "vue";
import type { Session, SessionSummary } from "../../types/agent";
import type { ChatFileItem, ChatMessageItem } from "../../types/chat";
import ChatSidebar from "./ChatSidebar.vue";
import ChatHeader from "./ChatHeader.vue";
import ChatArea from "./ChatArea.vue";
import InputArea from "./InputArea.vue";
import FilePreviewDrawer from "./FilePreviewDrawer.vue";
import DebugPanel from "./DebugPanel.vue";

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
  "fork-session": [nodeId: string];
  "rewind-session": [nodeId: string];
}>();

const debugOpen = ref(false);
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
</style>
