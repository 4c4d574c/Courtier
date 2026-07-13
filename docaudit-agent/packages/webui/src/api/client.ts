import type { Session, SessionSummary } from "../types/agent";

const API_BASE = "/api";

// ---------------------------------------------------------------------------
// Module-level token storage — set by useAuth via setApiToken().
// No circular dependency: useAuth imports setApiToken, client never imports useAuth.
// ---------------------------------------------------------------------------
let _token: string | null = null;

export function setApiToken(token: string | null) {
  _token = token;
}

function getAuthHeaders(): Record<string, string> {
  if (_token) return { Authorization: `Bearer ${_token}` };
  return {};
}

async function authFetch(
  url: string,
  init: RequestInit = {},
): Promise<Response> {
  const headers = getAuthHeaders();
  let res = await fetch(url, {
    ...init,
    headers: { ...init.headers, ...headers },
    credentials: "include",
  });

  // Auto-refresh on 401
  if (res.status === 401 && !url.includes("/auth/refresh")) {
    try {
      const refreshResp = await fetch(`${API_BASE}/auth/refresh`, {
        method: "POST",
        credentials: "include",
      });
      if (refreshResp.ok) {
        const data: { token: string } | null = await refreshResp
          .json()
          .catch(() => null);
        if (data?.token) {
          setApiToken(data.token);
        }
        // Retry original request
        const newHeaders = getAuthHeaders();
        res = await fetch(url, {
          ...init,
          headers: { ...init.headers, ...newHeaders },
          credentials: "include",
        });
      }
    } catch {
      // Refresh failed — caller handles 401
    }
  }
  return res;
}

// ---------------------------------------------------------------------------
// API helpers
// ---------------------------------------------------------------------------
async function request(
  method: string,
  path: string,
  timeoutMs = 10_000,
): Promise<Response> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await authFetch(`${API_BASE}${path}`, {
      method,
      signal: controller.signal,
    });
    if (!res.ok) throw new Error(`${method} ${path} failed: ${res.status}`);
    return res;
  } finally {
    clearTimeout(timer);
  }
}

async function post(path: string): Promise<void> {
  await request("POST", path);
}

function qs(params: Record<string, string | undefined>): string {
  const filtered = Object.entries(params).filter(([, v]) => v) as [
    string,
    string,
  ][];
  if (filtered.length === 0) return "";
  return "?" + new URLSearchParams(filtered).toString();
}

export const api = {
  // ---- Auth ----
  async login(
    username: string,
    password: string,
  ): Promise<{ token: string; user: any }> {
    const res = await fetch(`${API_BASE}/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      body: JSON.stringify({ username, password }),
    });
    if (!res.ok) {
      const err: { detail?: string } = await res.json().catch(() => ({}));
      throw new Error(err.detail || `Login failed: ${res.status}`);
    }
    return res.json();
  },

  async register(
    username: string,
    email: string,
    password: string,
  ): Promise<void> {
    const res = await fetch(`${API_BASE}/auth/register`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, email, password }),
    });
    if (!res.ok) {
      const err: { detail?: string } = await res.json().catch(() => ({}));
      throw new Error(err.detail || `Register failed: ${res.status}`);
    }
  },

  async refreshToken(): Promise<{ token: string; user: any } | null> {
    const res = await fetch(`${API_BASE}/auth/refresh`, {
      method: "POST",
      credentials: "include",
    });
    if (!res.ok) return null;
    return res.json();
  },

  async logout(): Promise<void> {
    await fetch(`${API_BASE}/auth/logout`, {
      method: "POST",
      credentials: "include",
    });
  },

  // ---- Admin ----
  async listUsers(
    params: {
      page?: number;
      page_size?: number;
      search?: string;
      role?: string;
      status?: string;
    } = {},
  ): Promise<{ items: any[]; total: number; page: number; page_size: number }> {
    const strParams: Record<string, string> = {};
    if (params.page) strParams.page = String(params.page);
    if (params.page_size) strParams.page_size = String(params.page_size);
    if (params.search) strParams.search = params.search;
    if (params.role) strParams.role = params.role;
    if (params.status) strParams.status = params.status;
    const res = await authFetch(`${API_BASE}/admin/users${qs(strParams)}`);
    if (!res.ok) throw new Error(`Failed: ${res.status}`);
    return res.json();
  },

  async updateUser(
    id: number,
    data: { role?: string; status?: string; password?: string; email?: string },
  ): Promise<void> {
    const res = await authFetch(`${API_BASE}/admin/users/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });
    if (!res.ok) throw new Error(`Failed: ${res.status}`);
  },

  async listApprovals(): Promise<{
    items: {
      id: number;
      username: string;
      email: string;
      created_at: string;
    }[];
  }> {
    const res = await authFetch(`${API_BASE}/admin/approvals`);
    if (!res.ok) throw new Error(`Failed: ${res.status}`);
    return res.json();
  },

  async approveUser(id: number): Promise<void> {
    const res = await authFetch(`${API_BASE}/admin/approvals/${id}/approve`, {
      method: "POST",
    });
    if (!res.ok) throw new Error(`Failed: ${res.status}`);
  },

  async rejectUser(id: number): Promise<void> {
    const res = await authFetch(`${API_BASE}/admin/approvals/${id}/reject`, {
      method: "POST",
    });
    if (!res.ok) throw new Error(`Failed: ${res.status}`);
  },

  // ---- Profile ----
  async getProfile(): Promise<any> {
    const res = await authFetch(`${API_BASE}/profile`);
    if (!res.ok) throw new Error(`Failed: ${res.status}`);
    return res.json();
  },

  async updateProfile(data: {
    email?: string;
    current_password?: string;
    new_password?: string;
  }): Promise<void> {
    const res = await authFetch(`${API_BASE}/profile`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });
    if (!res.ok) throw new Error(`Failed: ${res.status}`);
  },

  // ---- Existing methods (now using authFetch with cookies + token) ----
  async uploadFile(file: File): Promise<{ fileId: string }> {
    const formData = new FormData();
    formData.append("file", file);
    const headers = getAuthHeaders();
    const res = await fetch(`${API_BASE}/files`, {
      method: "POST",
      body: formData,
      headers,
      credentials: "include",
    });
    if (!res.ok) throw new Error(`Upload failed: ${res.status}`);
    return res.json();
  },

  pause: () => post("/pause"),
  resume: () => post("/resume"),

  async stop(
    sessionId?: string,
  ): Promise<{ status: string; sessionId?: string; stopped: boolean }> {
    const res = await request("POST", `/stop${qs({ sessionId })}`);
    return res.json();
  },

  async listSessions(): Promise<SessionSummary[]> {
    const res = await authFetch(`${API_BASE}/sessions`);
    if (!res.ok) throw new Error(`GET /sessions failed: ${res.status}`);
    return res.json();
  },

  async loadSession(id: string): Promise<Session | null> {
    const res = await authFetch(`${API_BASE}/sessions/${id}`);
    if (!res.ok) return null;
    return res.json();
  },

  async deleteSession(id: string): Promise<void> {
    await request("DELETE", `/sessions/${id}`);
  },

  async createEventSource(params: {
    task?: string;
    fileId?: string;
    sessionId?: string;
  }): Promise<EventSource> {
    const headers = getAuthHeaders();
    const token = headers.Authorization?.slice(7) || "";
    const url = `${API_BASE}/sessions${qs({
      task: params.task,
      fileId: params.fileId,
      sessionId: params.sessionId,
      token,
    })}`;
    return new EventSource(url);
  },
};
