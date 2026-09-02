/**
 * Display names and sub-group layout for the admin settings page.
 *
 * Pure data + pure functions, no Vue imports, so they are unit-testable
 * from scripts/test-settings-form.mjs alongside settingsForm.ts.
 *
 * Chinese labels live here because backend field descriptions are
 * inconsistent (some missing, some English, some padded with an
 * "（环境变量: …）" tail). A field missing from FIELD_DISPLAY_NAMES
 * gracefully falls back to its env_name — never blank.
 */

/** Structural subset of SettingsField — keeps this file dependency-free. */
export interface LabeledField {
  name: string;
  env_name: string;
}

/** field name → Chinese display name. */
export const FIELD_DISPLAY_NAMES: Record<string, string> = {
  // model — 模型池
  llm_model_pool: "模型池",
  llm_endpoint_keys: "接入点密钥",
  // model — LLM 接入
  llm_base_url: "API 端点 URL",
  llm_api_key: "API 密钥",
  llm_model: "模型名称",
  llm_context_window_tokens: "上下文窗口大小（token）",
  llm_max_tokens: "单次回复最大 token",
  llm_temperature: "采样温度",
  llm_frequency_penalty: "频率惩罚",
  llm_presence_penalty: "存在惩罚",
  llm_timeout: "请求超时（秒）",
  llm_extra_body: "额外请求体参数（JSON）",
  // model — Embedding
  llm_embedding_model: "Embedding 模型",
  llm_embedding_base_url: "Embedding 端点 URL",
  llm_embedding_api_key: "Embedding 密钥",
  llm_embedding_dim: "Embedding 向量维度",
  llm_embedding_batch_size: "Embedding 批量大小",
  // model — 上下文管理
  context_budget_ratio: "全量压缩触发占比",
  context_compact_target_ratio: "压缩后目标占比",
  context_micro_compact_ratio: "微压缩触发占比",
  context_recent_tool_results_tokens: "微压缩工具结果保留预算",
  context_max_user_message_chars: "用户输入长度治理阈值",
  context_preview_max_chars: "大结果预览长度（字符）",
  // retrieval — Elasticsearch
  es_hosts: "服务地址",
  es_username: "用户名",
  es_password: "密码",
  es_index_chunks: "切片索引名",
  es_index_results: "结果索引名",
  // retrieval — MinIO
  minio_endpoint: "服务端点",
  minio_access_key: "Access Key",
  minio_secret_key: "Secret Key",
  minio_secure: "启用 HTTPS",
  minio_bucket_docs: "Bucket：docs",
  minio_bucket_library: "Bucket：library",
  minio_bucket_resources: "Bucket：resources",
  minio_bucket_documents: "Bucket：documents",
  minio_bucket_plugin_io: "插件中转 Bucket（24h 生命周期）",
  // retrieval — 搜索重排
  search_rerank_fetch: "重排候选数上限",
  search_rerank_candidate_budget_chars: "重排候选正文字符预算",
  // plugins
  courtier_plugin_endpoints: "插件端点映射",
  courtier_plugin_token: "插件共享鉴权 Token",
  // guards — 主循环
  loop_max_turns_without_business_artifacts: "连续无业务产出轮次上限",
  loop_max_consecutive_exploratory: "连续探索性工具调用上限",
  loop_max_same_tool_calls: "重复相同工具调用上限",
  loop_max_null_tool_results: "连续空工具结果上限",
  loop_max_reasoning_dupe_steps: "推理去重检测窗口",
  loop_reasoning_similarity_threshold: "推理相似度阈值",
  // guards — 子代理
  subagent_max_depth: "最大嵌套深度",
  subagent_max_turns: "单子代理最大思考轮数",
  subagent_max_runtime_seconds: "单子代理运行时长上限（秒）",
  subagent_max_cumulative_runtime_seconds: "子代理累计运行时长上限（秒）",
  subagent_max_total_spawns: "子代理派生总数上限",
  // guards — 并发与运行
  max_runs_per_user: "每用户并发运行上限",
  max_total_runs: "全局并发运行上限",
  run_grace_seconds: "运行结束事件保留宽限期（秒）",
  run_log_max_bytes: "单运行事件日志上限（字节）",
  run_log_max_events: "单运行事件日志条数上限",
  // observability — 链路追踪
  otel_service_name: "服务名",
  otel_exporter_otlp_endpoint: "OTLP 上报端点",
  otel_log_level: "日志级别",
  // observability — 日志与审计
  audit_log_enabled: "结构化审计日志",
  logger_level: "日志级别",
  // web — 认证与安全
  jwt_secret: "JWT 签名密钥",
  jwt_algorithm: "JWT 签名算法",
  jwt_expire_seconds: "Token 有效期（秒）",
  jwt_access_expire_seconds: "Access Token 有效期（秒）",
  bcrypt_rounds: "bcrypt 哈希轮数",
  // web — CORS
  cors_origins: "允许的跨域来源",
  cors_allow_credentials: "允许携带凭证",
};

/** Display name: Chinese label → env name → raw name. */
export function fieldDisplayName(field: LabeledField): string {
  return FIELD_DISPLAY_NAMES[field.name] ?? field.env_name ?? field.name;
}

/**
 * Backend descriptions pad ops-relevant text with a "（环境变量: X）"
 * tail; the env name already gets its own meta line in the UI.
 * Returns "" when nothing beyond the tail remains.
 */
export function cleanDescription(description: string): string {
  return description
    .replace(/（环境变量[:：][^）]*）/g, "")
    .replace(/\s*。\s*。/g, "。")
    .trim();
}

export interface SettingsGroup<F extends LabeledField> {
  key: string;
  label: string;
  fields: F[];
}

interface GroupSpec {
  key: string;
  label: string;
  /** Field-name prefixes (first match wins, so order longer prefixes first). */
  prefixes?: string[];
  /** Exact field names. */
  names?: string[];
}

/**
 * Fields removed from the admin UI on purpose — their values stay in the
 * DB / env and keep working, they just have no editable row.
 *
 * LLM 接入的端点三件套：模型池启用后对聊天链不再生效（仅作池清空时的
 * 兜底、升级播种源、embedding 回退端点），展示出来只会和模型池形成
 * "两个模型配置入口"的混淆。改值走 env 覆盖或直接改库。
 */
export const HIDDEN_SETTING_FIELDS: ReadonlySet<string> = new Set([
  "llm_base_url",
  "llm_api_key",
  "llm_model",
]);

/**
 * Ordered sub-groups per category. Unknown fields land in a trailing
 * "其他" group so newly added backend fields are never dropped (hidden
 * fields are the one explicit exception — filtered before grouping).
 */
const CATEGORY_GROUPS: Record<string, GroupSpec[]> = {
  model: [
    // Pool first: exact names must win over the broad "llm_" prefix below.
    { key: "pool", label: "模型池", names: ["llm_model_pool", "llm_endpoint_keys"] },
    { key: "embedding", label: "Embedding 向量", prefixes: ["llm_embedding_"] },
    { key: "llm", label: "全局默认参数", prefixes: ["llm_"] },
    { key: "context", label: "上下文管理", prefixes: ["context_"] },
  ],
  retrieval: [
    { key: "es", label: "Elasticsearch", prefixes: ["es_"] },
    { key: "minio", label: "MinIO 对象存储", prefixes: ["minio_"] },
    { key: "rerank", label: "搜索重排", prefixes: ["search_rerank_"] },
  ],
  plugins: [{ key: "conn", label: "插件连接", prefixes: ["courtier_plugin_"] }],
  guards: [
    { key: "loop", label: "主循环守卫", prefixes: ["loop_"] },
    { key: "subagent", label: "子代理", prefixes: ["subagent_"] },
    { key: "runs", label: "并发与运行", prefixes: ["run_"], names: ["max_runs_per_user", "max_total_runs"] },
  ],
  observability: [
    { key: "otel", label: "链路追踪", prefixes: ["otel_"] },
    { key: "logs", label: "日志与审计", prefixes: ["audit_log_"], names: ["logger_level"] },
  ],
  web: [
    { key: "auth", label: "认证与安全", prefixes: ["jwt_"], names: ["bcrypt_rounds"] },
    { key: "cors", label: "CORS 跨域", prefixes: ["cors_"] },
  ],
};

function matchGroup(specs: GroupSpec[], name: string): GroupSpec | null {
  for (const spec of specs) {
    if (spec.prefixes?.some((p) => name.startsWith(p))) return spec;
    if (spec.names?.includes(name)) return spec;
  }
  return null;
}

/** Split one category's fields into ordered sub-groups (empty groups dropped). */
export function groupCategoryFields<F extends LabeledField>(
  category: string,
  fields: F[],
): Array<SettingsGroup<F>> {
  const specs = CATEGORY_GROUPS[category] ?? [];
  const groups = new Map<string, SettingsGroup<F>>();
  for (const spec of specs) groups.set(spec.key, { key: spec.key, label: spec.label, fields: [] });
  const other: SettingsGroup<F> = { key: "other", label: "其他", fields: [] };

  for (const field of fields) {
    if (HIDDEN_SETTING_FIELDS.has(field.name)) continue;
    const spec = matchGroup(specs, field.name);
    const group = spec ? groups.get(spec.key) : null;
    (group ?? other).fields.push(field);
  }
  const ordered = specs.map((spec) => groups.get(spec.key)!).filter((g) => g.fields.length > 0);
  return other.fields.length ? [...ordered, other] : ordered;
}

/** One-line responsibility blurb shown under each category title. */
export const CATEGORY_DESCRIPTIONS: Record<string, string> = {
  model: "模型接入参数与上下文窗口管理",
  retrieval: "Elasticsearch 检索与 MinIO 对象存储",
  plugins: "插件端点与通道鉴权",
  guards: "代理循环、子代理与并发守卫",
  observability: "链路追踪与审计日志",
  web: "认证、会话与跨域安全",
};
