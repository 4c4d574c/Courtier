import type { Session, SessionSummary } from "../types/agent";

const API_BASE = "/api";

export interface ApiUser {
  id: number;
  username: string;
  email: string;
  role: "admin" | "auditor";
  status: "pending" | "active" | "disabled";
}

export interface AdminUser extends ApiUser {
  created_at: string;
}

export interface Profile {
  id: number;
  username: string;
  email: string;
  role: "admin" | "auditor";
  status: "pending" | "active" | "disabled";
}

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

// Single-flight refresh: concurrent 401s share one refresh request, so a
// rotated refresh token is never presented twice (which would trip the
// backend's reuse detection and revoke the whole token family).
let _refreshPromise: Promise<boolean> | null = null;

function refreshAccessToken(): Promise<boolean> {
  if (!_refreshPromise) {
    _refreshPromise = (async () => {
      try {
        const refreshResp = await fetch(`${API_BASE}/auth/refresh`, {
          method: "POST",
          credentials: "include",
        });
        if (!refreshResp.ok) return false;
        const data: { token: string } | null = await refreshResp
          .json()
          .catch(() => null);
        if (data?.token) {
          setApiToken(data.token);
          return true;
        }
        return false;
      } catch {
        // Refresh failed — caller handles 401
        return false;
      }
    })().finally(() => {
      _refreshPromise = null;
    });
  }
  return _refreshPromise;
}

async function authFetch(
  url: string,
  init: RequestInit = {},
  timeoutMs = 10_000,
): Promise<Response> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const headers = getAuthHeaders();
    let res = await fetch(url, {
      ...init,
      headers: { ...init.headers, ...headers },
      credentials: "include",
      signal: controller.signal,
    });

    // Auto-refresh on 401
    if (res.status === 401 && !url.includes("/auth/refresh")) {
      if (await refreshAccessToken()) {
        // Retry original request
        const newHeaders = getAuthHeaders();
        res = await fetch(url, {
          ...init,
          headers: { ...init.headers, ...newHeaders },
          credentials: "include",
          signal: controller.signal,
        });
      }
    }
    return res;
  } finally {
    clearTimeout(timer);
  }
}

// Shared error parser: prefer the backend's `detail` message over a bare
// status code so the UI can show actionable errors.
async function parseErrorDetail(res: Response, fallback: string): Promise<Error> {
  const err: { detail?: unknown } = await res.json().catch(() => ({}));
  const detail = err.detail;
  if (typeof detail === "string" && detail) {
    return new Error(detail);
  }
  if (Array.isArray(detail) && detail.length > 0) {
    // FastAPI/Pydantic 422: detail is a list of {loc, msg, type} objects.
    const first = detail[0] as { msg?: string } | undefined;
    if (first?.msg) return new Error(first.msg);
  }
  return new Error(`${fallback}: ${res.status}`);
}

// ---------------------------------------------------------------------------
// API helpers
// ---------------------------------------------------------------------------
async function request(
  method: string,
  path: string,
  timeoutMs = 10_000,
): Promise<Response> {
  const res = await authFetch(`${API_BASE}${path}`, { method }, timeoutMs);
  if (!res.ok) throw await parseErrorDetail(res, `${method} ${path} failed`);
  return res;
}

async function post(path: string): Promise<void> {
  await request("POST", path);
}

async function postJson<T = unknown>(path: string, body: unknown): Promise<T> {
  const res = await authFetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw await parseErrorDetail(res, `POST ${path} failed`);
  return res.json() as Promise<T>;
}

function qs(params: Record<string, string | undefined>): string {
  const filtered = Object.entries(params).filter(
    ([, v]) => v !== undefined && v !== null,
  ) as [string, string][];
  if (filtered.length === 0) return "";
  return "?" + new URLSearchParams(filtered).toString();
}

export const api = {
  // ---- Auth ----
  async login(
    username: string,
    password: string,
  ): Promise<{ token: string; expires_in: number; user: ApiUser }> {
    const res = await fetch(`${API_BASE}/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      body: JSON.stringify({ username, password }),
    });
    if (!res.ok) throw await parseErrorDetail(res, "Login failed");
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
    if (!res.ok) throw await parseErrorDetail(res, "Register failed");
  },

  async refreshToken(): Promise<{ token: string; expires_in: number; user: ApiUser } | null> {
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
  ): Promise<{ items: AdminUser[]; total: number; page: number; page_size: number }> {
    const strParams: Record<string, string> = {};
    if (params.page) strParams.page = String(params.page);
    if (params.page_size) strParams.page_size = String(params.page_size);
    if (params.search) strParams.search = params.search;
    if (params.role) strParams.role = params.role;
    if (params.status) strParams.status = params.status;
    const res = await authFetch(`${API_BASE}/admin/users${qs(strParams)}`);
    if (!res.ok) throw await parseErrorDetail(res, "Request failed");
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
    if (!res.ok) throw await parseErrorDetail(res, "Request failed");
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
    if (!res.ok) throw await parseErrorDetail(res, "Request failed");
    return res.json();
  },

  async approveUser(id: number): Promise<void> {
    const res = await authFetch(`${API_BASE}/admin/approvals/${id}/approve`, {
      method: "POST",
    });
    if (!res.ok) throw await parseErrorDetail(res, "Request failed");
  },

  async rejectUser(id: number): Promise<void> {
    const res = await authFetch(`${API_BASE}/admin/approvals/${id}/reject`, {
      method: "POST",
    });
    if (!res.ok) throw await parseErrorDetail(res, "Request failed");
  },

  // ---- Profile ----
  async getProfile(): Promise<Profile> {
    const res = await authFetch(`${API_BASE}/profile`);
    if (!res.ok) throw await parseErrorDetail(res, "Request failed");
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
    if (!res.ok) throw await parseErrorDetail(res, "Request failed");
  },

  // ---- Existing methods (now using authFetch with cookies + token) ----
  async uploadFile(file: File): Promise<{ fileId: string }> {
    const formData = new FormData();
    formData.append("file", file);
    // Larger timeout for document uploads.
    const res = await authFetch(
      `${API_BASE}/files`,
      {
        method: "POST",
        body: formData,
      },
      120_000,
    );
    if (!res.ok) throw await parseErrorDetail(res, "Upload failed");
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
    if (!res.ok) throw await parseErrorDetail(res, "GET /sessions failed");
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
    // SSE authenticates via the httpOnly access_token cookie set at login —
    // EventSource cannot set an Authorization header, and a ?token= query
    // param would leak the JWT into browser history and server access logs.
    const url = `${API_BASE}/sessions${qs({
      task: params.task,
      fileId: params.fileId,
      sessionId: params.sessionId,
    })}`;
    return new EventSource(url, { withCredentials: true });
  },

  async forkSession(
    sessionId: string,
    nodeId?: string,
    reason?: string,
  ): Promise<{ new_node_id: string; messages: unknown[] }> {
    return postJson<{ new_node_id: string; messages: unknown[] }>(
      `/sessions/${sessionId}/fork`,
      { node_id: nodeId, reason },
    );
  },

  async rewindSession(
    sessionId: string,
    nodeId: string,
  ): Promise<{ current_node_id: string; messages: unknown[] }> {
    return postJson<{ current_node_id: string; messages: unknown[] }>(
      `/sessions/${sessionId}/rewind`,
      { node_id: nodeId },
    );
  },
};
