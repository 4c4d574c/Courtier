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

  async createEventSource(params: {
    task?: string;
    fileId?: string;
    sessionId?: string;
    editTurn?: number;
  }): Promise<EventSource> {
    // SSE authenticates via the httpOnly access_token cookie set at login —
    // EventSource cannot set an Authorization header, and a ?token= query
    // param would leak the JWT into browser history and server access logs.
    const url = `${API_BASE}/sessions${qs({
      task: params.task,
      fileId: params.fileId,
      sessionId: params.sessionId,
      editTurn:
        params.editTurn !== undefined ? String(params.editTurn) : undefined,
    })}`;
    return new EventSource(url, { withCredentials: true });
  },

  /**
   * Attach to a session's active run: the server replays events after
   * *since* (the snapshot's eventSeq watermark) then streams live until the
   * run terminates. Used when restoring a still-running session.
   */
  attachSessionEvents(sessionId: string, since: number): EventSource {
    const url = `${API_BASE}/sessions/${sessionId}/events${qs({
      since: String(since),
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

  // ---- Admin: extension management (plugins & skills) ----
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
