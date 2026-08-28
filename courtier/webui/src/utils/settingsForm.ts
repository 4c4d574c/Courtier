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
