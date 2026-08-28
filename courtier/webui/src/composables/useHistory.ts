import { ref } from "vue";
import type { Session, SessionSummary } from "../types/agent";
import { normalizeSession } from "../utils/toolCalls";
import { createTrailingThrottle } from "../utils/throttle";
import { api } from "../api/client";

const LIST_REFRESH_THROTTLE_MS = 5000;

export function useHistory() {
  const sessions = ref<SessionSummary[]>([]);
  const loading = ref(false);
  /** Non-empty = the last list refresh failed (old list kept rendered). */
  const listError = ref<string | null>(null);
  let listErrorTimer: ReturnType<typeof setTimeout> | null = null;

  function showListError(message: string) {
    listError.value = message;
    // Auto-clear so a stale hint never outlives its cause by long.
    if (listErrorTimer !== null) clearTimeout(listErrorTimer);
    listErrorTimer = setTimeout(() => {
      listErrorTimer = null;
      listError.value = null;
    }, 10000);
  }

  async function fetchSessions() {
    loading.value = true;
    try {
      // Assignment only on success — a failed refresh keeps the old list
      // rendered instead of flashing empty (the old code let the 429/500
      // escape and left the sidebar on "暂无历史会话" after a page load).
      sessions.value = await api.listSessions();
      listError.value = null;
    } catch (e: unknown) {
      showListError(
        (e as { status?: number } | null)?.status === 429
          ? "刷新过于频繁，会话列表暂时未更新"
          : "会话列表刷新失败，显示的是上次结果",
      );
    } finally {
      loading.value = false;
    }
  }

  // Run-status events for background sessions can arrive in bursts (several
  // runs finishing together); coalesce their list refreshes — the list route
  // is rate-limited.
  const throttledRefresh = createTrailingThrottle(() => {
    void fetchSessions();
  }, LIST_REFRESH_THROTTLE_MS);

  async function loadSession(id: string): Promise<Session | null> {
    const session = await api.loadSession(id);
    if (!session) return null;
    return normalizeSession(session);
  }

  async function deleteSession(id: string) {
    await api.deleteSession(id);
  }

  async function updateSession(
    id: string,
    patch: { task?: string; pinned?: boolean },
  ): Promise<SessionSummary> {
    return api.updateSession(id, patch);
  }

  return {
    sessions,
    loading,
    listError,
    fetchSessions,
    refreshSessionsThrottled: throttledRefresh,
    loadSession,
    deleteSession,
    updateSession,
  };
}
