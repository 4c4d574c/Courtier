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
  type: "string" | "int" | "float" | "bool" | "list" | "json" | "enum";
  /** Allowed values for type === "enum" (Literal fields); absent otherwise. */
  choices?: string[];
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


/** 工具确认名单行（tool_confirmation 的结构化编辑模型）。 */
export interface ToolConfirmationRow {
  tool: string;
  message: string;
}

/** 把设置里的 JSON 文本解析为确认名单行（解析失败返回空）。 */
export function parseToolConfirmationRows(raw: string): ToolConfirmationRow[] {
  let value: unknown;
  try {
    value = JSON.parse(raw || "[]");
  } catch {
    return [];
  }
  if (!Array.isArray(value)) return [];
  return value.map((entry) => ({
    tool: typeof entry?.tool === "string" ? entry.tool : "",
    message: typeof entry?.message === "string" ? entry.message : "",
  }));
}

/** 行序列化回设置值（JSON 文本）；无工具名的行跳过；message 为空则省略。 */
export function serializeToolConfirmationRows(
  rows: ToolConfirmationRow[],
): string {
  const value = rows
    .map((row) => ({
      tool: row.tool.trim(),
      message: row.message.trim(),
    }))
    .filter((row) => row.tool !== "");
  return JSON.stringify(
    value.map((row) =>
      row.message ? { tool: row.tool, message: row.message } : { tool: row.tool }
    )
  );
}

/** 行校验：有提示语但没工具名 = 半行。 */
export function toolConfirmationRowError(row: ToolConfirmationRow): string {
  const tool = row.tool.trim();
  const message = row.message.trim();
  if (!tool && message) return "缺少工具名";
  return "";
}

/** 守卫声明行（guardrail_guards 的结构化编辑模型）。 */
export interface GuardRow {
  name: string;
  classPath: string;
  scope: "session" | "run";
  enabled: boolean;
  builtin: boolean;
}

/**
 * 五个内置基线，与服务端种子一致。仅作「恢复默认」的初值；builtin 条目
 * 的身份字段以服务端权威为准（保存时被强制回种子值），此处不一致只会被
 * 静默纠正，不会破坏运行语义。
 */
export const DEFAULT_GUARD_ROWS: GuardRow[] = [
  {
    name: "tool_disabled",
    classPath: "courtier.agent.core.guardrails.permission_guards.ToolDisabledGuard",
    scope: "session",
    enabled: true,
    builtin: true,
  },
  {
    name: "path_policy",
    classPath: "courtier.agent.core.guardrails.permission_guards.PathPolicyGuard",
    scope: "session",
    enabled: true,
    builtin: true,
  },
  {
    name: "confirmation",
    classPath: "courtier.agent.core.guardrails.confirmation.ConfirmationGuard",
    scope: "session",
    enabled: true,
    builtin: true,
  },
  {
    name: "explore_loop",
    classPath: "courtier.agent.core.guardrails.loop_guardrails.ExploreLoopGuard",
    scope: "run",
    enabled: true,
    builtin: true,
  },
  {
    name: "business_artifact",
    classPath: "courtier.agent.core.guardrails.loop_guardrails.BusinessArtifactProgressGuard",
    scope: "run",
    enabled: true,
    builtin: true,
  },
];

/** 把设置里的 JSON 文本解析为守卫声明行（解析失败返回空）。 */
export function parseGuardRows(raw: string): GuardRow[] {
  let value: unknown;
  try {
    value = JSON.parse(raw || "[]");
  } catch {
    return [];
  }
  if (!Array.isArray(value)) return [];
  return value.map((entry) => ({
    name: typeof entry?.name === "string" ? entry.name : "",
    classPath: typeof entry?.class_path === "string" ? entry.class_path : "",
    scope: entry?.scope === "run" ? "run" : "session",
    enabled: entry?.enabled !== false,
    builtin: entry?.builtin === true,
  }));
}

/** 行序列化回设置值（JSON 文本）；缺名称或类路径的行跳过。 */
export function serializeGuardRows(rows: GuardRow[]): string {
  const value = rows
    .map((row) => ({
      name: row.name.trim(),
      class_path: row.classPath.trim(),
      scope: row.scope,
      enabled: row.enabled,
      builtin: row.builtin,
    }))
    .filter((row) => row.name !== "" && row.class_path !== "");
  return JSON.stringify(value);
}

/** 行校验：有名称没类路径（或反之）= 半行。 */
export function guardRowError(row: GuardRow): string {
  const name = row.name.trim();
  const classPath = row.classPath.trim();
  if (!name && !classPath) return "";
  if (!name) return "缺少守卫名称";
  if (!classPath) return "缺少类路径";
  return "";
}

/** 把设置里的 JSON 文本解析为字符串名单（tools_disabled；解析失败返回空）。 */
export function parseStringList(raw: string): string[] {
  let value: unknown;
  try {
    value = JSON.parse(raw || "[]");
  } catch {
    return [];
  }
  if (!Array.isArray(value)) return [];
  return value.map((item) => String(item));
}

/** 序列化字符串名单；空项与首尾空白剔除，保序去重。 */
export function serializeStringList(items: string[]): string {
  const seen: string[] = [];
  for (const item of items) {
    const value = item.trim();
    if (value && !seen.includes(value)) seen.push(value);
  }
  return JSON.stringify(seen);
}
