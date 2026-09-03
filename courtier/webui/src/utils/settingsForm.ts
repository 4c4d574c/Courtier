/**
 * Pure form logic for the admin settings page.
 *
 * Kept free of component/Vue dependencies so the semantics (masked
 * secrets, changed-only submit bodies, per-type coercion, explicit
 * clears) are unit-testable from scripts/test-settings-form.mjs.
 */

export interface SettingsField {
  name: string;
  category: string;
  is_secret: boolean;
  effect: "hot" | "rebuild" | "restart";
  source: "db" | "env" | "default";
  value: unknown;
  type: "string" | "int" | "float" | "bool" | "list" | "json";
  env_name: string;
  description: string;
}

export interface SettingsCategory {
  key: string;
  label: string;
  fields: SettingsField[];
}

export interface SecretView {
  set: boolean;
  tail?: string;
  unreadable?: boolean;
}

export type FormValue = string | boolean;
export type FormState = Record<string, FormValue>;

/** Editable representation of every field; secrets start empty (= unchanged). */
export function initFormState(fields: SettingsField[]): FormState {
  const state: FormState = {};
  for (const field of fields) {
    if (field.is_secret) {
      state[field.name] = "";
      continue;
    }
    if (field.type === "bool") {
      state[field.name] = Boolean(field.value);
      continue;
    }
    const value = field.value;
    state[field.name] =
      value == null
        ? ""
        : field.type === "list" || field.type === "json"
          ? JSON.stringify(value)
          : String(value);
  }
  return state;
}

/** Placeholder for a secret input, mirroring the masked server view. */
export function secretPlaceholder(field: SettingsField): string {
  const view = field.value as SecretView | null;
  if (!view || !view.set) return "未设置";
  if (view.unreadable) return "已设置（不可读：加密密钥不匹配）";
  return `已配置（••••${view.tail ?? ""}）— 留空保持不变`;
}

export interface UpdateBodyResult {
  /** PUT body: only changed fields; null = clear back to env/default. */
  body: Record<string, unknown>;
  /** field -> coercion error message (submit must be blocked). */
  errors: Record<string, string>;
}

/** Build the changed-only PUT body plus coercion errors. */
export function buildUpdateBody(
  fields: SettingsField[],
  state: FormState,
  cleared: Record<string, boolean> = {},
): UpdateBodyResult {
  const body: Record<string, unknown> = {};
  const errors: Record<string, string> = {};

  for (const field of fields) {
    if (cleared[field.name]) {
      body[field.name] = null;
      continue;
    }
    const raw = state[field.name];

    if (field.is_secret) {
      if (typeof raw === "string" && raw !== "") body[field.name] = raw;
      continue;
    }

    switch (field.type) {
      case "bool": {
        const parsed = Boolean(raw);
        if (parsed !== Boolean(field.value)) body[field.name] = parsed;
        break;
      }
      case "int": {
        const parsed = Number(raw);
        if (!Number.isFinite(parsed) || !Number.isInteger(parsed)) {
          errors[field.name] = "必须是整数";
          break;
        }
        if (parsed !== Number(field.value)) body[field.name] = parsed;
        break;
      }
      case "float": {
        const parsed = Number(raw);
        if (!Number.isFinite(parsed)) {
          errors[field.name] = "必须是数字";
          break;
        }
        if (parsed !== Number(field.value)) body[field.name] = parsed;
        break;
      }
      case "list": {
        const text = typeof raw === "string" ? raw : "";
        if (text === "") break;
        let parsed: unknown;
        try {
          parsed = JSON.parse(text);
        } catch {
          errors[field.name] = "必须是 JSON 数组";
          break;
        }
        if (!Array.isArray(parsed)) {
          errors[field.name] = "必须是 JSON 数组";
          break;
        }
        if (JSON.stringify(parsed) !== JSON.stringify(field.value ?? [])) {
          body[field.name] = parsed;
        }
        break;
      }
      case "json": {
        const text = typeof raw === "string" ? raw : "";
        if (text === "") break;
        let parsed: unknown;
        try {
          parsed = JSON.parse(text);
        } catch {
          errors[field.name] = "必须是合法 JSON";
          break;
        }
        if (JSON.stringify(parsed) !== JSON.stringify(field.value ?? null)) {
          body[field.name] = parsed;
        }
        break;
      }
      default: {
        const parsed = String(raw ?? "");
        if (parsed !== String(field.value ?? "")) body[field.name] = parsed;
      }
    }
  }
  return { body, errors };
}

/** 工具路径白名单行（tool_path_policies 的结构化编辑模型）。 */
export interface ToolPathRow {
  tool: string;
  exempt: boolean;
  /** 允许的路径，一条一项 */
  paths: string[];
}

/** 把设置里的 JSON 文本解析为按工具的行（解析失败返回空 = 无有效声明）。 */
export function parseToolPathRows(raw: string): ToolPathRow[] {
  let value: unknown;
  try {
    value = JSON.parse(raw || "{}");
  } catch {
    return [];
  }
  if (value == null || typeof value !== "object") return [];
  return Object.entries(value as Record<string, unknown>).map(([tool, v]) => ({
    tool,
    exempt: v === false,
    paths: Array.isArray(v) ? v.map((p) => String(p)) : [],
  }));
}

/** 行序列化回设置值（JSON 文本）；无工具名的行跳过；路径逐条去空白。 */
export function serializeToolPathRows(rows: ToolPathRow[]): string {
  const value: Record<string, unknown> = {};
  for (const row of rows) {
    const tool = row.tool.trim();
    if (!tool) continue;
    if (row.exempt) {
      value[tool] = false;
      continue;
    }
    value[tool] = row.paths.map((p) => p.trim()).filter((p) => p !== "");
  }
  return JSON.stringify(value);
}

/** 行校验：豁免行或完整行合法；有工具名但既不豁免又无路径 = 半行。 */
export function toolPathRowError(row: ToolPathRow): string {
  const tool = row.tool.trim();
  const paths = row.paths.map((p) => p.trim()).filter(Boolean);
  if (!tool && paths.length === 0) return "";
  if (!tool) return "缺少工具名";
  if (!row.exempt && paths.length === 0) return "需要至少一个路径，或勾选豁免";
  return "";
}
