<template>
  <ChatLayout
    :title="title"
    :session="session"
    :messages="messages"
    :sessions="historySessions"
    :history-loading="historyLoading"
    :is-running="isRunning"
    :uploading="uploading"
    :upload-error="uploadError"
    :drawer-open="drawerOpen"
    :current-file="currentFile"
    :sidebar-open="sidebarOpen"
    :username="user?.username"
    :user-role="user?.role"
    @new-session="newSessionWithCleanup"
    @history-select="handleHistorySelect"
    @history-delete="handleHistoryDelete"
    @history-rename="handleHistoryRename"
    @history-pin="handleHistoryPin"
    @logout="handleLogout"
    @toggle-sidebar="sidebarOpen = !sidebarOpen"
    @toggle-theme="toggleTheme"
    @preview-file="openPreview"
    @close-preview="closePreview"
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
import { useTheme } from "../composables/useTheme";
import { api } from "../api/client";
import type { ChatFileRecord } from "../types/chat";
import ChatLayout from "../components/chat/ChatLayout.vue";

const router = useRouter();
const { user, logout } = useAuth();
const {
  session,
  connect,
  disconnect,
  newSession,
  restoreSession,
  stop,
  isRunning,
} = useAgentSession();
const {
  sessions: historySessions,
  loading: historyLoading,
  fetchSessions,
  loadSession,
  deleteSession,
  updateSession,
} = useHistory();
const filePreview = useFilePreview();
const { isOpen: drawerOpen, currentFile, open: openPreview, close: closePreview } =
  filePreview;
const { initTheme, toggleTheme } = useTheme();

// Desktop starts with the history sidebar open; mobile keeps it closed
// (it renders as an overlay there).
const sidebarOpen = ref(window.matchMedia("(min-width: 769px)").matches);
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

async function handleHistoryRename(id: string, task: string) {
  const previous = historySessions.value;
  historySessions.value = historySessions.value.map((s) =>
    s.id === id ? { ...s, task } : s,
  );
  try {
    await updateSession(id, { task });
    // 正在查看的会话被改名时同步顶部标题
    if (session.id === id) session.task = task;
  } catch (_err) {
    historySessions.value = previous;
    console.warn("重命名会话失败", _err);
  }
}

async function handleHistoryPin(id: string, pinned: boolean) {
  const previous = historySessions.value;
  historySessions.value = [...historySessions.value]
    .map((s) => (s.id === id ? { ...s, pinned } : s))
    .sort(
      (a, b) =>
        Number(b.pinned ?? false) - Number(a.pinned ?? false) ||
        b.createdAt - a.createdAt,
    );
  try {
    await updateSession(id, { pinned });
  } catch (_err) {
    historySessions.value = previous;
    console.warn("置顶会话失败", _err);
  }
}

async function handleHistorySelect(id: string) {
  try {
    const loaded = await loadSession(id);
    if (loaded) {
      revokeUploadedFiles();
      restoreSession(loaded);
      // Auto-close only on mobile, where the sidebar is an overlay.
      if (!window.matchMedia("(min-width: 769px)").matches) {
        sidebarOpen.value = false;
      }
    }
  } catch (e: unknown) {
    uploadError.value = e instanceof Error ? e.message : "加载会话失败";
  }
}

async function handleLogout() {
  await logout();
  router.push("/login");
}

onMounted(() => {
  fetchSessions();
  initTheme();
});

onUnmounted(() => {
  // Close the SSE stream — otherwise it keeps writing into this (now
  // orphaned) session state until the backend finishes.
  disconnect();
  revokeUploadedFiles();
});
</script>
