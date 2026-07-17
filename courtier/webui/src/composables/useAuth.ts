import { reactive, computed } from "vue";
import { setApiToken, api, type ApiUser } from "../api/client";

interface AuthState {
  user: ApiUser | null;
  initialized: boolean;
}

const state = reactive<AuthState>({
  user: null,
  initialized: false,
});

let refreshTimer: ReturnType<typeof setTimeout> | null = null;

const REFRESH_LEEWAY = 0.8;

function clearRefreshTimer() {
  if (refreshTimer) {
    clearTimeout(refreshTimer);
    refreshTimer = null;
  }
}

function scheduleRefresh(expiresIn: number) {
  clearRefreshTimer();
  const delayMs = Math.max(1000, Math.floor(expiresIn * REFRESH_LEEWAY * 1000));
  refreshTimer = setTimeout(async () => {
    try {
      const resp = await api.refreshToken();
      if (resp) {
        setApiToken(resp.token);
        state.user = resp.user;
        scheduleRefresh(resp.expires_in);
      } else {
        setApiToken(null);
        state.user = null;
      }
    } catch (err) {
      console.warn("auto refresh failed:", err);
    }
  }, delayMs);
}

export function useAuth() {
  const isAdmin = computed(() => state.user?.role === "admin");
  const isLoggedIn = computed(() => state.user !== null);

  async function initAuth(): Promise<void> {
    if (state.initialized) return;
    state.initialized = true;
    try {
      const resp = await api.refreshToken();
      if (resp) {
        setApiToken(resp.token);
        state.user = resp.user;
        scheduleRefresh(resp.expires_in);
      }
    } catch (err) {
      // Not logged in or network error — stay on guest page.
      console.warn("initAuth failed:", err);
    }
  }

  async function login(username: string, password: string): Promise<ApiUser> {
    const resp = await api.login(username, password);
    setApiToken(resp.token);
    state.user = resp.user;
    scheduleRefresh(resp.expires_in);
    return resp.user;
  }

  async function register(
    username: string,
    email: string,
    password: string,
  ): Promise<void> {
    await api.register(username, email, password);
  }

  async function logout(): Promise<void> {
    clearRefreshTimer();
    try {
      await api.logout();
    } catch (err) {
      console.warn("logout request failed:", err);
    }
    setApiToken(null);
    state.user = null;
  }

  return {
    user: computed(() => state.user),
    isAdmin,
    isLoggedIn,
    initAuth,
    login,
    register,
    logout,
  };
}
