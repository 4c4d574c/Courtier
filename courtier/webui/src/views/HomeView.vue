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
    :drawer-open="drawerOpen"
    :current-file="currentFile"
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
import { api } from "../api/client";
import type { ChatFileRecord } from "../types/chat";
import ChatLayout from "../components/chat/ChatLayout.vue";

const router = useRouter();
const { user, logout } = useAuth();
const { session, connect, newSession, restoreSession, stop, isRunning } =
  useAgentSession();
const {
  sessions: historySessions,
  loading: historyLoading,
  fetchSessions,
  loadSession,
  deleteSession,
} = useHistory();
const filePreview = useFilePreview();
const { isOpen: drawerOpen, currentFile, open: openPreview, close: closePreview } =
  filePreview;

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
  try {
    const loaded = await loadSession(id);
    if (loaded) {
      revokeUploadedFiles();
      restoreSession(loaded);
      sidebarOpen.value = false;
    }
  } catch (e: unknown) {
    uploadError.value = e instanceof Error ? e.message : "加载会话失败";
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
