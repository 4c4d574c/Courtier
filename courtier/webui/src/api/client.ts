import type { Session, SessionSummary } from "../types/agent";

import { SseFetchClient } from "../utils/sseStream";

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

/** Current in-memory Bearer token (for the SSE fetch transport). */
export function getApiToken(): string | null {
  return _token;
}

function getAuthHeaders(): Record<string, string> {
  if (_token) return { Authorization: `Bearer ${_token}` };
  return {};
}

// Single-flight refresh: concurrent 401s share one refresh request, so a
// rotated refresh token is never presented twice (which would trip the
// backend's reuse detection and revoke the whole token family).  Every
// refresh path (authFetch 401s, useAuth timers, SSE onerror) goes through
// here — never POST /auth/refresh directly.
type RefreshDetail = { token: string; expires_in: number; user: ApiUser };

let _refreshPromise: Promise<RefreshDetail | null> | null = null;

function refreshAccessToken(): Promise<RefreshDetail | null> {
  if (!_refreshPromise) {
    _refreshPromise = (async () => {
      try {
        const refreshResp = await fetch(`${API_BASE}/auth/refresh`, {
          method: "POST",
          credentials: "include",
        });
        if (!refreshResp.ok) return null;
        const data: RefreshDetail | null = await refreshResp
          .json()
          .catch(() => null);
        if (data?.token) {
          setApiToken(data.token);
          return data;
        }
        return null;
      } catch (err) {
        // Network-level failure: rethrow so callers can distinguish
        // "definitely not logged in" (null) from "try again later".
        // HTTP non-2xx above already returns null.
        if (err instanceof TypeError) throw err;
        return null;
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
      try {
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
      } catch {
        // Refresh itself hit the network wall — surface the original 401.
      }
    }
    return res;
  } finally {
    clearTimeout(timer);
  }
}

// Shared error parser: prefer the backend's `detail` message over a bare
// status code so the UI can show actionable errors. The HTTP status is
// attached as `status` for callers that need to branch on it (e.g. 429).
async function parseErrorDetail(res: Response, fallback: string): Promise<Error> {
  const err: { detail?: unknown } = await res.json().catch(() => ({}));
  const detail = err.detail;
  let message: string;
  if (typeof detail === "string" && detail) {
    message = detail;
  } else if (Array.isArray(detail) && detail.length > 0) {
    // FastAPI/Pydantic 422: detail is a list of {loc, msg, type} objects.
    const first = detail[0] as { msg?: string } | undefined;
    message = first?.msg ?? `${fallback}: ${res.status}`;
  } else if (detail && typeof detail === "object" && "message" in detail) {
    // Structured app error — surface field/target-level details so the
    // admin can act on them.  errors is either a validation array
    // [{field,message}] or a probe-failure map {es: "...", minio: "..."}.
    const obj = detail as {
      message?: string;
      errors?: Record<string, string> | Array<{ field?: string; message?: string }>;
    };
    let parts: string[] = [];
    if (Array.isArray(obj.errors)) {
      parts = obj.errors.map((e2) => `${e2.field ?? "?"}: ${e2.message ?? ""}`);
    } else if (obj.errors && typeof obj.errors === "object") {
      parts = Object.entries(obj.errors).map(([k, v]) => `${k}: ${v}`);
    }
    message = [obj.message, ...parts].filter(Boolean).join("；") || `${fallback}: ${res.status}`;
  } else {
    message = `${fallback}: ${res.status}`;
  }
  const error = new Error(message) as Error & { status?: number };
  error.status = res.status;
  return error;
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

async function putJson<T = unknown>(path: string, body: unknown): Promise<T> {
  const res = await authFetch(`${API_BASE}${path}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw await parseErrorDetail(res, `PUT ${path} failed`);
  return res.json() as Promise<T>;
}

function qs(params: Record<string, string | undefined>): string {
  const filtered = Object.entries(params).filter(
    ([, v]) => v !== undefined && v !== null,
  ) as [string, string][];
  if (filtered.length === 0) return "";
  return "?" + new URLSearchParams(filtered).toString();
}

export interface DeploymentField {
  name: string;
  env_name: string;
  value: string | { set: boolean };
  description: string;
}

export interface SettingsView {
  mode: "db" | "env";
  version: number | null;
  unreadable: string[];
  categories: import("../utils/settingsForm").SettingsCategory[];
  deployment?: DeploymentField[];
}

export interface SettingsUpdateResult {
  applied: string[];
  cleared: string[];
  version: number;
  restart_required: string[];
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

  async setupStatus(): Promise<{ setup_required: boolean }> {
    const res = await fetch(`${API_BASE}/setup/status`);
    if (!res.ok) throw await parseErrorDetail(res, "GET /api/setup/status failed");
    return res.json();
  },

  async createAdmin(body: {
    username: string;
    password: string;
    email?: string;
    setup_key?: string;
  }): Promise<{ ok: boolean }> {
    const res = await fetch(`${API_BASE}/setup/admin`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw await parseErrorDetail(res, "创建管理员失败");
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

  refreshToken(): Promise<{ token: string; expires_in: number; user: ApiUser } | null> {
    // Single-flight: see refreshAccessToken — a raw second POST could
    // present the rotated refresh cookie twice and trip the backend's
    // reuse detection (revoking the whole token family).
    return refreshAccessToken();
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
  async uploadFile(file: File): Promise<{
    fileId: string;
    kind?: string;
    durationSeconds?: number | null;
    width?: number | null;
    height?: number | null;
  }> {
    const formData = new FormData();
    formData.append("file", file);
    // Larger timeout: media uploads may include server-side video transcoding.
    const res = await authFetch(
      `${API_BASE}/files`,
      {
        method: "POST",
        body: formData,
      },
      600_000,
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

  async resolveConfirmation(
    sessionId: string,
    confirmationId: string,
    decision: "approve" | "approve_session" | "deny",
  ): Promise<{
    confirmationId: string;
    decision: string;
    toolName?: string;
    alreadyResolved?: boolean;
  }> {
    return postJson(
      `/sessions/${sessionId}/confirmations/${confirmationId}`,
      { decision },
    );
  },

  /**
   * Resolve a resource-library document to a viewable PDF URL.
   * DOCX/TXT/MD are converted on demand server-side (cached in MinIO).
   * `originalUrl` points at the uploaded original file (same as `url` for PDFs).
   */
  async getResourcePdfUrl(
    resourceId: number,
  ): Promise<{ url: string; converted: boolean; originalUrl?: string | null }> {
    const res = await authFetch(`${API_BASE}/resources/${resourceId}/pdf`);
    if (!res.ok) throw await parseErrorDetail(res, "GET /resources/:id/pdf failed");
    return res.json();
  },

  async updateSession(
    id: string,
    patch: { task?: string; pinned?: boolean },
  ): Promise<SessionSummary> {
    const res = await authFetch(`${API_BASE}/sessions/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    });
    if (!res.ok) throw await parseErrorDetail(res, "PATCH /sessions/:id failed");
    return res.json();
  },

  /**
   * POST run-start via the fetch SSE transport: the task rides in the JSON
   * body (out of access logs and browser history).  Reconnect is disabled —
   * re-POSTing would duplicate the run; on break the caller reloads the
   * snapshot and resumes through attachSessionEvents.
   */
  createEventSource(params: {
    task?: string;
    fileId?: string;
    fileIds?: string;
    sessionId?: string;
    editTurn?: number;
    modelId?: string;
  }): SseFetchClient {
    const token = getApiToken();
    return new SseFetchClient(`${API_BASE}/sessions/run`, {
      method: "POST",
      body: {
        task: params.task ?? "",
        fileId: params.fileId,
        fileIds: params.fileIds,
        sessionId: params.sessionId,
        editTurn: params.editTurn,
        modelId: params.modelId,
      },
      reconnect: false,
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    });
  },

  /** Model pool public view for the selector (no endpoint internals). */
  async getModels(): Promise<{
    endpoints: Array<{
      endpointId: string;
      endpointName: string;
      models: Array<{ id: string; name: string; modalities?: string[] }>;
    }>;
    defaultModelId: string;
  }> {
    const res = await authFetch(`${API_BASE}/models`);
    if (!res.ok) throw await parseErrorDetail(res, "GET /models failed");
    return res.json();
  },

  /**
   * Attach to a session's active run: the server replays events after
   * *since* (the snapshot's eventSeq watermark) then streams live until the
   * run terminates. Used when restoring a still-running session.
   */
  attachSessionEvents(sessionId: string, since: number): SseFetchClient {
    // Attach is read-only and resume-safe: the client reconnects with the
    // Last-Event-ID header on mid-stream breaks (native-EventSource-like).
    const token = getApiToken();
    return new SseFetchClient(
      `${API_BASE}/sessions/${sessionId}/events${qs({ since: String(since) })}`,
      {
        reconnect: true,
        lastEventId: String(since),
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      },
    );
  },

  /** Global run-status channel (one per app lifetime; auth via cookie). */
  createGlobalEventsChannel(): SseFetchClient {
    // Reconnect is driven by the composable's backoff timer (it also
    // realigns the list once the channel is healthy), so reconnect stays off.
    const token = getApiToken();
    return new SseFetchClient(`${API_BASE}/events`, {
      reconnect: false,
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    });
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

  async compactSession(sessionId: string): Promise<{
    beforeTokens: number;
    afterTokens: number;
    beforeMessages: number;
    afterMessages: number;
    compactCount: number;
  }> {
    return postJson(`/sessions/${sessionId}/compact`, {});
  },

  // ---- Resource library ----
  async uploadResource(
    file: File,
    meta: {
      title?: string;
      author?: string;
      source?: string;
      tags?: string;
      publishDate?: string;
      visibility?: "public" | "personal";
    },
  ): Promise<{
    id: number;
    title: string;
    chunkCount: number;
    charCount: number;
    visibility: string;
    status: string;
  }> {
    const form = new FormData();
    form.append("file", file);
    if (meta.title) form.append("title", meta.title);
    if (meta.author) form.append("author", meta.author);
    if (meta.source) form.append("source", meta.source);
    if (meta.tags) form.append("tags", meta.tags);
    if (meta.publishDate) form.append("publish_date", meta.publishDate);
    if (meta.visibility) form.append("visibility", meta.visibility);
    const res = await authFetch(`${API_BASE}/resources/upload`, {
      method: "POST",
      body: form,
    });
    if (!res.ok) throw await parseErrorDetail(res, "POST /resources/upload failed");
    return res.json();
  },

  async listResources(
    query = "",
    skip = 0,
    limit = 50,
    scope: "all" | "public" | "personal" = "all",
  ): Promise<{ total: number; items: ResourceSummary[] }> {
    const res = await authFetch(
      `${API_BASE}/resources/list${qs({ query: query || undefined, skip: String(skip), limit: String(limit), scope })}`,
    );
    if (!res.ok) throw await parseErrorDetail(res, "GET /resources/list failed");
    return res.json();
  },

  async deleteResource(id: number): Promise<void> {
    await request("DELETE", `/resources/${id}`);
  },

  // ---- Account deletion ----
  async submitDeletionRequest(password: string): Promise<DeletionRequest> {
    const res = await authFetch(`${API_BASE}/profile/deletion-request`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password }),
    });
    if (!res.ok) throw await parseErrorDetail(res, "POST /profile/deletion-request failed");
    return res.json();
  },

  async getMyDeletionRequest(): Promise<{ request: DeletionRequest | null }> {
    const res = await authFetch(`${API_BASE}/profile/deletion-request`);
    if (!res.ok) throw await parseErrorDetail(res, "GET /profile/deletion-request failed");
    return res.json();
  },

  async cancelDeletionRequest(): Promise<void> {
    await request("DELETE", `/profile/deletion-request`);
  },

  async listDeletionRequests(): Promise<{ items: DeletionRequestItem[] }> {
    const res = await authFetch(`${API_BASE}/admin/deletion-requests`);
    if (!res.ok) throw await parseErrorDetail(res, "GET /admin/deletion-requests failed");
    return res.json();
  },

  async approveDeletionRequest(id: number): Promise<{ receipt: DeletionReceipt }> {
    const res = await authFetch(`${API_BASE}/admin/deletion-requests/${id}/approve`, {
      method: "POST",
    });
    if (!res.ok) throw await parseErrorDetail(res, "POST /admin/deletion-requests/:id/approve failed");
    return res.json();
  },

  async rejectDeletionRequest(id: number): Promise<DeletionRequest> {
    const res = await authFetch(`${API_BASE}/admin/deletion-requests/${id}/reject`, {
      method: "POST",
    });
    if (!res.ok) throw await parseErrorDetail(res, "POST /admin/deletion-requests/:id/reject failed");
    return res.json();
  },

  async deleteUserAccount(id: number): Promise<{ receipt: DeletionReceipt }> {
    const res = await authFetch(`${API_BASE}/admin/users/${id}`, { method: "DELETE" });
    if (!res.ok) throw await parseErrorDetail(res, "DELETE /admin/users/:id failed");
    return res.json();
  },

  // ---- Memory (layered DB-backed memory) ----
  async listMemory(scope: "global" | "mine", domain = "", query = ""): Promise<MemoryEntry[]> {
    const res = await authFetch(
      `${API_BASE}/memory/${scope}${qs({ domain: domain || undefined, query: query || undefined })}`,
    );
    if (!res.ok) throw await parseErrorDetail(res, "GET /memory failed");
    return res.json();
  },

  async upsertMemory(
    scope: "global" | "mine",
    body: { title: string; content: string; domain?: string },
  ): Promise<MemoryEntry> {
    const res = await authFetch(`${API_BASE}/memory/${scope}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw await parseErrorDetail(res, "POST /memory failed");
    return res.json();
  },

  async updateMemory(
    scope: "global" | "mine",
    id: number,
    body: { title?: string; content?: string; domain?: string },
  ): Promise<MemoryEntry> {
    const res = await authFetch(`${API_BASE}/memory/${scope}/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw await parseErrorDetail(res, "PATCH /memory failed");
    return res.json();
  },

  async deleteMemory(scope: "global" | "mine", id: number): Promise<void> {
    await request("DELETE", `/memory/${scope}/${id}`);
  },

  async clearMemory(scope: "global" | "mine"): Promise<{ cleared: number }> {
    const res = await request("DELETE", `/memory/${scope}`);
    return res.json();
  },

  async listMemoryDomains(): Promise<{ domains: string[] }> {
    const res = await authFetch(`${API_BASE}/memory/domains`);
    if (!res.ok) throw await parseErrorDetail(res, "GET /memory/domains failed");
    return res.json();
  },

  async listMemoryChanges(): Promise<MemoryChange[]> {
    const res = await authFetch(`${API_BASE}/admin/memory/changes`);
    if (!res.ok) throw await parseErrorDetail(res, "GET /admin/memory/changes failed");
    return res.json();
  },

  // ---- Admin: extension management (plugins & skills) ----
  async getSettings(): Promise<SettingsView> {
    const res = await authFetch(`${API_BASE}/admin/settings`);
    if (!res.ok) throw await parseErrorDetail(res, "GET /admin/settings failed");
    return res.json();
  },

  async listKnownToolNames(): Promise<{ tools: string[] }> {
    const res = await authFetch(`${API_BASE}/admin/settings/tool-names`);
    if (!res.ok) throw await parseErrorDetail(res, "GET /admin/settings/tool-names failed");
    return res.json();
  },

  async updateSettings(
    category: string,
    body: Record<string, unknown>,
  ): Promise<SettingsUpdateResult> {
    const res = await authFetch(
      `${API_BASE}/admin/settings/${category}`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
      // Saving probes live LLM/ES/MinIO connections — the default 10s
      // aborts valid-but-slow configurations.
      60_000,
    );
    if (!res.ok) throw await parseErrorDetail(res, "PUT /admin/settings failed");
    return res.json();
  },

  async testConnection(
    target: "llm" | "es" | "minio" | "plugins",
    body: Record<string, unknown> = {},
  ): Promise<{
    ok: boolean;
    error?: string;
    model?: string;
    reply?: string;
    errors?: Record<string, string>;
  }> {
    const res = await authFetch(
      `${API_BASE}/admin/settings/test/${target}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
      // The server really calls the LLM (expects a reply) and probes
      // ES/MinIO — 60s instead of the default 10s.
      60_000,
    );
    if (!res.ok) throw await parseErrorDetail(res, "POST /admin/settings/test failed");
    return res.json();
  },

  async rotateJwtSecret(): Promise<{ ok: boolean; sessions_invalidated: boolean }> {
    const res = await authFetch(`${API_BASE}/admin/settings/jwt/rotate`, {
      method: "POST",
    });
    if (!res.ok) throw await parseErrorDetail(res, "POST /admin/settings/jwt/rotate failed");
    return res.json();
  },

  async listPlugins(): Promise<{ items: PluginInfo[] }> {
    const res = await authFetch(`${API_BASE}/admin/extensions/plugins`);
    if (!res.ok) throw await parseErrorDetail(res, "GET /admin/extensions/plugins failed");
    return res.json();
  },

  async pluginAction(name: string, action: "start" | "stop" | "restart"): Promise<{ name: string; state: string }> {
    return postJson(`/admin/extensions/plugins/${name}/action`, { action });
  },

  async getPluginLogs(name: string, tail = 200): Promise<PluginLogs> {
    const res = await authFetch(
      `${API_BASE}/admin/extensions/plugins/${name}/logs?tail=${tail}`,
    );
    // 410 Gone: plugins are standalone services now — the host no longer
    // tees their stderr.  Surface the explanation as pane content instead
    // of an error.
    if (res.status === 410) {
      const body: { detail?: unknown } = await res.json().catch(() => ({}));
      const detail = typeof body.detail === "string" ? body.detail : "";
      return {
        name,
        lines: [detail || "插件已独立部署，日志请查看插件所在主机的进程/容器日志。"],
        totalLines: 1,
        sizeBytes: 0,
        logPath: "",
      };
    }
    if (!res.ok) throw await parseErrorDetail(res, "GET plugin logs failed");
    return res.json();
  },

  async listSkillsAdmin(): Promise<{ domains: DomainSkills[] }> {
    const res = await authFetch(`${API_BASE}/admin/extensions/skills`);
    if (!res.ok) throw await parseErrorDetail(res, "GET /admin/extensions/skills failed");
    return res.json();
  },

  async setSkillEnabled(name: string, enabled: boolean, domain: string): Promise<void> {
    await postJson(`/admin/extensions/skills/${name}/enabled`, { enabled, domain });
  },

  async createSkill(payload: {
    name: string;
    domain: string;
    display_name?: string;
    description?: string;
    mode?: string;
    default_mode?: string;
    tools?: string[];
    skills?: string[];
    tags?: string[];
    system_prompt: string;
  }): Promise<{ name: string; source: string; warnings?: string[] }> {
    return postJson(`/admin/extensions/skills`, payload);
  },

  async getSkill(name: string, domain: string): Promise<SkillDetail> {
    const res = await authFetch(
      `${API_BASE}/admin/extensions/skills/${name}?domain=${encodeURIComponent(domain)}`,
    );
    if (!res.ok) throw await parseErrorDetail(res, "GET /admin/extensions/skills/:name failed");
    return res.json();
  },

  async updateSkill(
    name: string,
    payload: {
      domain: string;
      display_name?: string;
      description?: string;
      mode?: string;
      default_mode?: string;
      tools?: string[];
      skills?: string[];
      tags?: string[];
      system_prompt: string;
    },
  ): Promise<{ name: string; source: string; warnings?: string[] }> {
    return putJson(`/admin/extensions/skills/${name}`, payload);
  },

  async createDomain(payload: {
    name: string;
    title?: string;
    description?: string;
    locale?: string;
  }): Promise<{ name: string; title: string; issues: string[] }> {
    return postJson(`/admin/extensions/domains`, payload);
  },

  async setDomainEnabled(name: string, enabled: boolean): Promise<void> {
    await postJson(`/admin/extensions/domains/${name}/enabled`, { enabled });
  },
};

export interface DomainSkills {
  name: string;
  title: string;
  description: string;
  enabled: boolean;
  skillsPath: string;
  items: SkillInfo[];
  errors: string[];
}

export interface PluginToolInfo {
  name: string;
  displayName: string;
  description: string;
}

export interface PluginInfo {
  name: string;
  source: string;
  scanStatus: string;
  scanError: string | null;
  blockedReason?: string;
  state: string;
  version: string;
  restartCount: number;
  description: string;
  tools: PluginToolInfo[];
}

export interface PluginLogs {
  name: string;
  lines: string[];
  totalLines: number;
  sizeBytes: number;
  logPath: string;
}

export interface SkillInfo {
  name: string;
  displayName: string;
  description: string;
  enabled: boolean;
  mode: string;
  defaultMode: string;
  tools: string[];
  skills: string[];
  tags: string[];
  version: string;
  timeoutSeconds: number;
  source: string;
}

export interface SkillDetail extends SkillInfo {
  systemPrompt: string;
}

export interface MemoryEntry {
  id: number;
  layer: "global" | "user";
  ownerId: number;
  domain: string;
  title: string;
  content: string;
  createdBy: string;
  updatedBy: string;
  createdAt: string;
  updatedAt: string;
}

export interface MemoryChange {
  id: number;
  entryId: number;
  action: "create" | "update" | "delete";
  layer: "global" | "user";
  ownerId: number;
  domain: string;
  title: string;
  oldHash: string | null;
  newHash: string | null;
  actor: string;
  createdAt: string;
}

export interface DeletionRequest {
  id: number;
  userId: number;
  status: "pending" | "approved" | "rejected" | "cancelled" | "executed";
  requestedBy: "self" | "admin";
  decidedBy: string;
  createdAt: string | null;
  decidedAt: string | null;
}

export interface DeletionRequestItem extends DeletionRequest {
  username: string | null;
  userStatus: string | null;
}

export interface DeletionReceipt {
  counts: Record<string, number>;
  failures: Record<string, string>;
}

export interface ResourceSummary {
  id: number;
  title: string;
  author: string | null;
  source: string | null;
  tags: string | null;
  publishDate: string | null;
  fileType: string;
  fileSize: number;
  chunkCount: number;
  charCount: number;
  status: string;
  visibility: "public" | "personal";
  ownerId: number | null;
  createdAt: string | null;
}
