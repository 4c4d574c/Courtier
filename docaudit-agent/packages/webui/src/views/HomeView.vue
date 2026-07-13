<template>
  <Terminal
    :stats="session.stats"
    :turns="session.turns"
    :turn-version="turnVersion"
    :thoughts="session.thoughts"
    :is-running="isRunning"
    :step-count="session.steps.length"
    :total-tools="totalTools"
    :done-tools="doneTools"
    :issue-count="issueCount"
    :is-expanded="isExpanded"
    :toggle="toggle"
    :uploading="uploading"
    :upload-error="uploadError"
    :model-name="session.modelName"
    :history-open="historyOpen"
    :history-sessions="historySessions"
    :history-loading="historyLoading"
    :error-message="session.errorMessage"
    :stop-reason="session.stopReason"
    :username="user?.username"
    :user-role="user?.role"
    @export="handleExport"
    @history-toggle="historyOpen = !historyOpen"
    @history-select="handleHistorySelect"
    @history-delete="handleHistoryDelete"
    @new-session="newSession"
    @quick-task="(task: string) => handleSubmit(task)"
    @submit="handleSubmit"
    @stop="stop"
    @logout="handleLogout"
  />
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from "vue";
import { useRouter } from "vue-router";
import { useAgentSession } from "../composables/useAgentSession";
import { useHistory } from "../composables/useHistory";
import { useToolCard } from "../composables/useToolCard";
import { useAuth } from "../composables/useAuth";
import { api } from "../api/client";
import Terminal from "../components/Terminal.vue";

const router = useRouter();
const { user, logout } = useAuth();
const { session, connect, newSession, stop, isRunning, turnVersion } =
  useAgentSession();
const {
  sessions: historySessions,
  loading: historyLoading,
  fetchSessions,
  loadSession,
  deleteSession,
} = useHistory();
const { isExpanded, toggle } = useToolCard();

const historyOpen = ref(false);
const uploading = ref(false);
const uploadError = ref("");

const totalTools = computed(() =>
  session.steps.reduce((sum, s) => sum + s.tools.length, 0),
);

const doneTools = computed(() =>
  session.steps.reduce(
    (sum, s) =>
      sum +
      s.tools.filter(
        (t) =>
          t.status === "done" || t.status === "error" || t.status === "warning",
      ).length,
    0,
  ),
);

const issueCount = computed(() =>
  session.steps.reduce(
    (sum, s) =>
      sum +
      s.tools.filter((t) => t.status === "error" || t.status === "warning")
        .length,
    0,
  ),
);

async function handleSubmit(task: string, file?: File) {
  if (file) {
    uploading.value = true;
    uploadError.value = "";
    try {
      const result = await api.uploadFile(file);
      connect(task, result.fileId, file.name);
    } catch (e: unknown) {
      uploadError.value = e instanceof Error ? e.message : "上传失败";
    } finally {
      uploading.value = false;
    }
  } else {
    connect(task);
  }
}

function handleExport() {
  const data = JSON.stringify(session, null, 2);
  const blob = new Blob([data], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `audit-${session.id || "report"}.json`;
  a.click();
  URL.revokeObjectURL(url);
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
    Object.assign(session, loaded);
    historyOpen.value = false;
  }
}

async function handleLogout() {
  await logout();
  router.push("/login");
}

onMounted(() => {
  fetchSessions();
});
</script>
