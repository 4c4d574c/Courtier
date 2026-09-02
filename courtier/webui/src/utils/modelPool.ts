/**
 * Pure logic for the admin model-pool editor (系统设置 → 模型池).
 *
 * The stored surface is two settings: `llm_model_pool` (JSON:
 * {endpoints, default_model_id}) and `llm_endpoint_keys` (secret map
 * endpoint id → api key).  The server masks the key map per entry
 * ({set, tail}), so the UI never sees key material — it edits drafts
 * (new values) and removals (null) which the server merges entry-wise.
 *
 * No Vue imports — unit-testable from scripts/test-model-pool.mjs.
 */

export interface PoolModelRow {
  id: string;
  name: string;
  model: string;
  /** Text inputs; "" = unset (the scalar llm_* default applies). */
  context_window_tokens: string;
  max_tokens: string;
  temperature: string;
  /** Advanced per-model overrides, same "" = default semantics. */
  timeout_seconds: string;
  frequency_penalty: string;
  presence_penalty: string;
  /** Raw JSON object text; "" = unset. */
  extra_body: string;
}

export interface PoolEndpointRow {
  id: string;
  name: string;
  base_url: string;
  enabled: boolean;
  /** New key draft; "" = leave the stored key unchanged. */
  keyDraft: string;
  /** Server-side masked view of the stored key. */
  keySet: boolean;
  keyTail: string;
  /** Admin asked to delete the stored key (op: null). */
  keyRemove: boolean;
  /** Serialized endpoint at load time — detects per-row modifications. */
  savedSnapshot: string;
  models: PoolModelRow[];
}

interface PoolModelDoc {
  id: string;
  name: string;
  model: string;
  context_window_tokens?: number | null;
  max_tokens?: number | null;
  temperature?: number | null;
  timeout_seconds?: number | null;
  frequency_penalty?: number | null;
  presence_penalty?: number | null;
  extra_body?: Record<string, unknown> | null;
}

interface PoolEndpointDoc {
  id: string;
  name: string;
  base_url: string;
  enabled: boolean;
  models: PoolModelDoc[];
}

export interface PoolDoc {
  endpoints: PoolEndpointDoc[];
  default_model_id: string;
}

export type KeyView = { set: boolean; tail?: string; unreadable?: boolean };

/** 8 hex chars — collision odds are irrelevant at pool scale. */
export function genId(prefix: string): string {
  const hex = Math.floor(Math.random() * 0xffffffff)
    .toString(16)
    .padStart(8, "0");
  return `${prefix}_${hex}`;
}

export function emptyModelRow(): PoolModelRow {
  return {
    id: genId("mdl"),
    name: "",
    model: "",
    context_window_tokens: "",
    max_tokens: "",
    temperature: "",
    timeout_seconds: "",
    frequency_penalty: "",
    presence_penalty: "",
    extra_body: "",
  };
}

export function emptyEndpointRow(): PoolEndpointRow {
  return {
    id: genId("ep"),
    name: "",
    base_url: "",
    enabled: true,
    keyDraft: "",
    keySet: false,
    keyTail: "",
    keyRemove: false,
    savedSnapshot: "",
    models: [],
  };
}

function optText(value: number | null | undefined): string {
  return value == null ? "" : String(value);
}

/** Parse the settings view values into editor rows. */
export function parsePool(
  doc: PoolDoc | null | undefined,
  keysView: Record<string, KeyView> | null | undefined,
): { rows: PoolEndpointRow[]; defaultModelId: string } {
  const keys = keysView ?? {};
  const rows = (doc?.endpoints ?? []).map((ep) => {
    const row: PoolEndpointRow = {
      id: ep.id,
      name: ep.name,
      base_url: ep.base_url,
      enabled: ep.enabled,
      keyDraft: "",
      keySet: Boolean(keys[ep.id]?.set),
      keyTail: keys[ep.id]?.tail ?? "",
      keyRemove: false,
      savedSnapshot: "",
      models: (ep.models ?? []).map((m) => ({
        id: m.id,
        name: m.name,
        model: m.model,
        context_window_tokens: optText(m.context_window_tokens),
        max_tokens: optText(m.max_tokens),
        temperature: optText(m.temperature),
        timeout_seconds: optText(m.timeout_seconds),
        frequency_penalty: optText(m.frequency_penalty),
        presence_penalty: optText(m.presence_penalty),
        extra_body: m.extra_body == null ? "" : JSON.stringify(m.extra_body),
      })),
    };
    row.savedSnapshot = serializeEndpoint(row);
    return row;
  });
  return { rows, defaultModelId: doc?.default_model_id ?? "" };
}

function optNum(text: string): number | null {
  const trimmed = text.trim();
  if (trimmed === "") return null;
  const parsed = Number(trimmed);
  return Number.isFinite(parsed) ? parsed : null;
}

function invalidNumbers(row: PoolEndpointRow): boolean {
  return row.models.some((m) =>
    [
      m.context_window_tokens,
      m.max_tokens,
      m.temperature,
      m.timeout_seconds,
      m.frequency_penalty,
      m.presence_penalty,
    ].some((t) => t.trim() !== "" && !Number.isFinite(Number(t))),
  );
}

/** Serialize one endpoint (also the per-row change fingerprint). */
export function serializeEndpoint(row: PoolEndpointRow): string {
  return JSON.stringify({
    id: row.id,
    name: row.name.trim(),
    base_url: row.base_url.trim(),
    enabled: row.enabled,
    models: row.models.map((m) => {
      // Omit unset optional fields so an untouched pool round-trips to the
      // exact stored JSON (no phantom "已修改" right after load).
      const entry: Record<string, unknown> = {
        id: m.id,
        name: m.name.trim(),
        model: m.model.trim(),
      };
      const window_tokens = optNum(m.context_window_tokens);
      if (window_tokens !== null) entry.context_window_tokens = window_tokens;
      const max_tokens = optNum(m.max_tokens);
      if (max_tokens !== null) entry.max_tokens = max_tokens;
      const temperature = optNum(m.temperature);
      if (temperature !== null) entry.temperature = temperature;
      const timeout_seconds = optNum(m.timeout_seconds);
      if (timeout_seconds !== null) entry.timeout_seconds = timeout_seconds;
      const frequency_penalty = optNum(m.frequency_penalty);
      if (frequency_penalty !== null) entry.frequency_penalty = frequency_penalty;
      const presence_penalty = optNum(m.presence_penalty);
      if (presence_penalty !== null) entry.presence_penalty = presence_penalty;
      const rawExtra = m.extra_body.trim();
      if (rawExtra !== "") {
        try {
          const parsed = JSON.parse(rawExtra);
          if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
            entry.extra_body = parsed;
          }
        } catch {
          // Invalid JSON is surfaced by poolErrors; omitting it here keeps
          // the change fingerprint stable instead of throwing.
        }
      }
      return entry;
    }),
  });
}

/** Serialize rows back into the `llm_model_pool` JSON string. */
export function serializePool(rows: PoolEndpointRow[], defaultModelId: string): string {
  const doc: PoolDoc = {
    endpoints: rows.map((row) => {
      const parsed = JSON.parse(serializeEndpoint(row)) as PoolEndpointDoc;
      return parsed;
    }),
    default_model_id: defaultModelId,
  };
  return JSON.stringify(doc);
}

/**
 * Entry-level `llm_endpoint_keys` PUT ops: typed drafts overwrite,
 * removals delete; everything absent is kept server-side.
 */
export function buildKeyOps(rows: PoolEndpointRow[]): Record<string, string | null> {
  const ops: Record<string, string | null> = {};
  for (const row of rows) {
    if (row.keyDraft.trim() !== "") ops[row.id] = row.keyDraft.trim();
    else if (row.keyRemove) ops[row.id] = null;
  }
  return ops;
}

/** True when the keys map has pending changes (drives the dirty badge). */
export function keyOpsDirty(rows: PoolEndpointRow[]): boolean {
  return Object.keys(buildKeyOps(rows)).length > 0;
}

/** True when the endpoint is new or its non-key fields changed. */
export function endpointChanged(row: PoolEndpointRow): boolean {
  return row.savedSnapshot === "" || serializeEndpoint(row) !== row.savedSnapshot;
}

/**
 * Client-side validation mirroring the server's pool validator, so the
 * admin sees mistakes before a 422 round-trip.  Returns "" when valid.
 */
export function poolErrors(rows: PoolEndpointRow[], defaultModelId: string): string {
  const endpointIds = new Set<string>();
  const modelIds = new Set<string>();
  const allModelIds: string[] = [];
  for (const row of rows) {
    if (!row.id.trim()) return "接入点 id 不能为空";
    if (endpointIds.has(row.id)) return `接入点 id 重复: ${row.id}`;
    endpointIds.add(row.id);
    if (!row.name.trim()) return "接入点名称不能为空";
    if (!row.base_url.trim()) return "接入点 URL 不能为空";
    for (const model of row.models) {
      if (!model.id.trim()) return "模型 id 不能为空";
      if (modelIds.has(model.id)) return `模型 id 重复: ${model.id}`;
      modelIds.add(model.id);
      allModelIds.push(model.id);
      if (!model.name.trim()) return `模型 ${model.id} 的显示名不能为空`;
      if (!model.model.trim()) return `模型 ${model.id} 的模型 ID 不能为空`;
    }
    if (invalidNumbers(row)) {
      return "数字字段必须是数字（可留空）";
    }
    for (const model of row.models) {
      if (
        model.timeout_seconds.trim() !== "" &&
        Number(model.timeout_seconds) <= 0
      ) {
        return `模型 ${model.id} 的超时必须大于 0`;
      }
      for (const penalty of [model.frequency_penalty, model.presence_penalty]) {
        if (penalty.trim() !== "" && (Number(penalty) < -2 || Number(penalty) > 2)) {
          return `模型 ${model.id} 的惩罚系数需在 -2 ~ 2 之间`;
        }
      }
      if (model.temperature.trim() !== "" && Number(model.temperature) > 2) {
        return `模型 ${model.id} 的温度需在 0 ~ 2 之间`;
      }
      const rawExtra = model.extra_body.trim();
      if (rawExtra !== "") {
        try {
          const parsed = JSON.parse(rawExtra);
          if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
            return `模型 ${model.id} 的额外请求体必须是 JSON 对象`;
          }
        } catch {
          return `模型 ${model.id} 的额外请求体必须是合法 JSON`;
        }
      }
    }
  }
  if (allModelIds.length > 0 && !defaultModelId) {
    return "请选择默认模型";
  }
  if (defaultModelId && !allModelIds.includes(defaultModelId)) {
    return "默认模型不存在于池中";
  }
  return "";
}
