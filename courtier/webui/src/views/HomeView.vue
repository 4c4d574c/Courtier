<template>
  <ChatLayout
    :title="title"
    :session="session"
    :messages="messages"
    :sessions="historySessions"
    :history-loading="historyLoading"
    :list-error="historyListError"
    :is-running="isRunning"
    :uploading="uploading"
    :upload-error="uploadError"
    :drawer-open="drawerOpen"
    :current-file="currentFile"
    :current-citation="currentCitation"
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
    @preview-file="openPreview"
    @citation-click="openCitation"
    @close-preview="closePreview"
    @submit="handleSubmit"
    @stop="stop"
    @resolve-confirmation="resolveConfirmation"
    @edit-submit="handleEditSubmit"
    :can-edit="!isRunning"
    :edit-hint="editHint"
  />
  <RunToasts ref="runToastsRef" @select="handleToastSelect" />
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch } from "vue";
import { useRouter } from "vue-router";
import { useAgentSession } from "../composables/useAgentSession";
import { useHistory } from "../composables/useHistory";
import { useAuth } from "../composables/useAuth";
import { useChatMessages } from "../composables/useChatMessages";
import { useFilePreview } from "../composables/useFilePreview";
import { useTheme } from "../composables/useTheme";
import { useRunEvents } from "../composables/useRunEvents";
import { api } from "../api/client";
import { MESSAGES } from "../constants/messages";
import { fileRecordsFromTurns } from "../utils/chatMessages";
import type { ChatFileRecord } from "../types/chat";
import ChatLayout from "../components/chat/ChatLayout.vue";
import RunToasts from "../components/chat/RunToasts.vue";

const router = useRouter();
const { user, logout } = useAuth();
const {
  session,
  connect,
  disconnect,
  newSession,
  restoreSession,
  stop,
  editAndResend,
  isRunning,
  isCompacted,
  resolveConfirmation,
} = useAgentSession();
const {
  sessions: historySessions,
  loading: historyLoading,
  listError: historyListError,
  fetchSessions,
  refreshSessionsThrottled,
  loadSession,
  deleteSession,
  updateSession,
} = useHistory();
const filePreview = useFilePreview();
const {
  isOpen: drawerOpen,
  currentFile,
  currentCitation,
  open: openPreview,
  openCitation,
  close: closePreview,
} = filePreview;
const { initTheme } = useTheme();

// Desktop starts with the history sidebar open; mobile keeps it closed
// (it renders as an overlay there).
const sidebarOpen = ref(window.matchMedia("(min-width: 769px)").matches);
const uploading = ref(false);
const uploadError = ref("");
const uploadedFiles = ref<ChatFileRecord[]>([]);

const { messages, title } = useChatMessages(session, uploadedFiles);

// --- Global run-events: live sidebar badges + completion toasts ----------
const runEvents = useRunEvents();
const runToastsRef = ref<InstanceType<typeof RunToasts> | null>(null);

// The status dot must not lag: live run_status events override the
// (mount-time) list snapshot per session.
watch(
  () => ({ ...runEvents.statuses }),
  (live) => {
    for (const s of historySessions.value) {
      const entry = live[s.id];
      if (entry) {
        s.status = entry.status as typeof s.status;
        s.queuePosition = entry.queuePosition;
        if (entry.pendingConfirmations !== undefined) {
          s.pendingConfirmations = entry.pendingConfirmations;
        }
      }
    }
  },
  { deep: true },
);

runEvents.onRunStatus((event) => {
  // Update the sidebar entry even if it hasn't been listed yet. Refetches
  // are throttled — several background runs finishing together must not
  // burst the rate-limited list route.
  if (!historySessions.value.some((s) => s.id === event.sessionId)) {
    refreshSessionsThrottled();
    return;
  }
  // Toast only for background sessions (the open one streams its own end).
  const isOpenSession = session.id === event.sessionId;
  const terminal =
    event.status === "completed" ||
    event.status === "error" ||
    event.status === "stopped";
  if (terminal && !isOpenSession) {
    const listed = historySessions.value.find((s) => s.id === event.sessionId);
    runToastsRef.value?.push({
      sessionId: event.sessionId,
      kind: event.status as "completed" | "error" | "stopped",
      title:
        event.status === "completed"
          ? `后台任务完成:${listed?.task ?? event.sessionId}`
          : event.status === "error"
            ? `后台任务出错:${listed?.task ?? event.sessionId}`
            : `后台任务已停止:${listed?.task ?? event.sessionId}`,
      detail: event.conclusion,
    });
  }
});

// The channel has no replay — after a reconnect the sidebar realigns from
// the authoritative list.
runEvents.onReconnect(() => {
  void fetchSessions();
});

function handleToastSelect(sessionId: string) {
  void handleHistorySelect(sessionId);
}

// Compacted sessions keep the edit entry visible but disabled, with the
// reason as tooltip (their history is a summary — not turn-addressable).
const editHint = computed(() =>
  isCompacted.value ? MESSAGES.CHAT_EDIT_COMPACTED_HINT : "",
);

function handleEditSubmit({ turnIndex, text }: { turnIndex: number; text: string }) {
  if (isRunning.value || isCompacted.value) return;
  if (turnIndex === 0) {
    // Keep the sidebar title in sync with the edited first turn.
    const sid = session.id;
    historySessions.value = historySessions.value.map((s) =>
      s.id === sid ? { ...s, task: text } : s,
    );
  }
  editAndResend(turnIndex, text);
}

function clearUploadedFiles() {
  uploadedFiles.value = [];
}

function newSessionWithCleanup() {
  clearUploadedFiles();
  newSession();
}

async function handleSubmit(task: string, file?: File) {
  let fileId: string | undefined;

  if (file) {
    uploading.value = true;
    uploadError.value = "";
    try {
      const result = await api.uploadFile(file);
      fileId = result.fileId;
      uploadedFiles.value = [
        ...uploadedFiles.value,
        { fileId, name: file.name, url: `/api/files/${encodeURIComponent(fileId)}` },
      ];
    } catch (e: unknown) {
      uploadError.value = e instanceof Error ? e.message : "上传失败";
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
    // Deleting the open session: stop its run first, then drop the stream
    // and reset the main view exactly like starting a new session.
    if (session.id === id && isRunning.value) {
      await stop();
    }
    await deleteSession(id);
    if (session.id === id) {
      closePreview();
      newSessionWithCleanup();
    }
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
      uploadedFiles.value = fileRecordsFromTurns(loaded.turns);
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
  clearUploadedFiles();
});
</script>
