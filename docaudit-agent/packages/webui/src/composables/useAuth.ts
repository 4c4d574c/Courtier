import { reactive, computed } from "vue";
import { setApiToken, api } from "../api/client";

interface AuthUser {
  id: number;
  username: string;
  email: string;
  role: "admin" | "auditor";
  status: string;
}

interface AuthState {
  user: AuthUser | null;
  initialized: boolean;
}

const state = reactive<AuthState>({
  user: null,
  initialized: false,
});

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
      }
    } catch {
      // Not logged in — stay on guest page
    }
  }

  async function login(username: string, password: string): Promise<AuthUser> {
    const resp = await api.login(username, password);
    setApiToken(resp.token);
    state.user = resp.user;
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
    try {
      await api.logout();
    } catch {
      /* ignore */
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
