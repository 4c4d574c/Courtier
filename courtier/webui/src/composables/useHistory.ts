import { ref } from "vue";
import type { Session, SessionSummary } from "../types/agent";
import { normalizeSession } from "../utils/toolCalls";
import { api } from "../api/client";

export function useHistory() {
  const sessions = ref<SessionSummary[]>([]);
  const loading = ref(false);

  async function fetchSessions() {
    loading.value = true;
    try {
      sessions.value = await api.listSessions();
    } finally {
      loading.value = false;
    }
  }

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
    fetchSessions,
    loadSession,
    deleteSession,
    updateSession,
  };
}
