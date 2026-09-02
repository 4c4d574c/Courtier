import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { rmSync, mkdirSync, readFileSync, writeFileSync, readdirSync, statSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const scriptDir = dirname(fileURLToPath(import.meta.url));
const rootDir = resolve(scriptDir, "..");
const outDir = resolve(rootDir, ".tmp/settings-form-test");

function fixRelativeImports(dir) {
  for (const entry of readdirSync(dir)) {
    const fullPath = resolve(dir, entry);
    if (statSync(fullPath).isDirectory()) {
      fixRelativeImports(fullPath);
    } else if (fullPath.endsWith(".js")) {
      let source = readFileSync(fullPath, "utf8");
      source = source.replace(/from\s+["'](\.\/[^"']+?)(?!\.js)["']/g, 'from "$1.js"');
      source = source.replace(/from\s+["'](\.\.\/[^"']+?)(?!\.js)["']/g, 'from "$1.js"');
      writeFileSync(fullPath, source);
    }
  }
}

rmSync(outDir, { recursive: true, force: true });
mkdirSync(outDir, { recursive: true });

try {
  execFileSync(
    process.execPath,
    [
      resolve(rootDir, "node_modules/typescript/bin/tsc"),
      "--target", "es2022", "--module", "esnext", "--moduleResolution", "bundler",
      "--strict", "--noEmitOnError", "--outDir", outDir,
      resolve(rootDir, "src/utils/settingsForm.ts"),
    ],
    { stdio: "inherit" },
  );
  fixRelativeImports(outDir);

  execFileSync(
    process.execPath,
    [
      resolve(rootDir, "node_modules/typescript/bin/tsc"),
      "--target", "es2022", "--module", "esnext", "--moduleResolution", "bundler",
      "--strict", "--noEmitOnError", "--outDir", outDir,
      resolve(rootDir, "src/utils/settingsLabels.ts"),
    ],
    { stdio: "inherit" },
  );
  fixRelativeImports(outDir);

  const { initFormState, buildUpdateBody, secretPlaceholder } = await import(
    pathToFileURL(resolve(outDir, "settingsForm.js")).href
  );
  const {
    FIELD_DISPLAY_NAMES,
    fieldDisplayName,
    cleanDescription,
    groupCategoryFields,
  } = await import(pathToFileURL(resolve(outDir, "settingsLabels.js")).href);

  // --- initFormState ---
  {
    const state = initFormState([
      { name: "llm_model", category: "model", is_secret: false, effect: "hot", source: "env",
        value: "qwen", type: "string", env_name: "LLM_NAME", description: "" },
      { name: "llm_temperature", category: "model", is_secret: false, effect: "hot", source: "default",
        value: 0, type: "float", env_name: "LLM_TEMPERATURE", description: "" },
      { name: "llm_api_key", category: "model", is_secret: true, effect: "hot", source: "db",
        value: { set: true, tail: "abcd" }, type: "string", env_name: "LLM_API_KEY", description: "" },
      { name: "cors_origins", category: "web", is_secret: false, effect: "restart", source: "default",
        value: ["http://localhost:5173"], type: "list", env_name: "CORS_ORIGINS", description: "" },
      { name: "audit_log_enabled", category: "observability", is_secret: false, effect: "hot",
        source: "default", value: true, type: "bool", env_name: "AUDIT_LOG_ENABLED", description: "" },
    ]);
    assert.equal(state.llm_model, "qwen");
    assert.equal(state.llm_temperature, "0");
    assert.equal(state.llm_api_key, ""); // secrets start empty = unchanged
    assert.equal(state.cors_origins, '["http://localhost:5173"]');
    assert.equal(state.audit_log_enabled, true);
  }

  // --- secretPlaceholder ---
  assert.equal(secretPlaceholder(
    { name: "x", category: "m", is_secret: true, effect: "hot", source: "env",
      value: { set: false }, type: "string", env_name: "X", description: "" },
  ), "未设置");
  assert.match(secretPlaceholder(
    { name: "x", category: "m", is_secret: true, effect: "hot", source: "env",
      value: { set: true, tail: "wxyz" }, type: "string", env_name: "X", description: "" },
  ), /••••wxyz/);
  assert.match(secretPlaceholder(
    { name: "x", category: "m", is_secret: true, effect: "hot", source: "env",
      value: { set: true, unreadable: true }, type: "string", env_name: "X", description: "" },
  ), /不可读/);

  // --- buildUpdateBody: changed-only semantics ---
  const fields = [
    { name: "llm_model", category: "model", is_secret: false, effect: "hot", source: "env",
      value: "qwen", type: "string", env_name: "LLM_NAME", description: "" },
    { name: "llm_temperature", category: "model", is_secret: false, effect: "hot", source: "default",
      value: 0, type: "float", env_name: "LLM_TEMPERATURE", description: "" },
    { name: "llm_api_key", category: "model", is_secret: true, effect: "hot", source: "default",
      value: { set: false }, type: "string", env_name: "LLM_API_KEY", description: "" },
    { name: "max_total_runs", category: "guards", is_secret: false, effect: "hot", source: "default",
      value: 20, type: "int", env_name: "MAX_TOTAL_RUNS", description: "" },
    { name: "cors_origins", category: "web", is_secret: false, effect: "restart", source: "default",
      value: ["http://a"], type: "list", env_name: "CORS_ORIGINS", description: "" },
    { name: "llm_extra_body", category: "model", is_secret: false, effect: "hot", source: "default",
      value: null, type: "json", env_name: "LLM_EXTRA_BODY", description: "" },
  ];

  {
    const { body, errors } = buildUpdateBody(fields, {
      llm_model: "qwen",            // unchanged → omitted
      llm_temperature: "0.4",       // changed float
      llm_api_key: "sk-new",        // secret set
      max_total_runs: "7",          // changed int
      cors_origins: '["http://b"]', // changed list
      llm_extra_body: "",           // empty json → omitted
    });
    assert.deepEqual(errors, {});
    assert.deepEqual(body, {
      llm_temperature: 0.4,
      llm_api_key: "sk-new",
      max_total_runs: 7,
      cors_origins: ["http://b"],
    });
  }

  {
    // Secret left empty = unchanged (never submitted as "").
    const { body } = buildUpdateBody(fields, {
      llm_model: "deepseek", llm_temperature: "0", llm_api_key: "", max_total_runs: "20",
      cors_origins: '["http://a"]', llm_extra_body: "",
    });
    assert.deepEqual(body, { llm_model: "deepseek" });
  }

  {
    // Explicit clear wins over any typed value.
    const { body } = buildUpdateBody(
      fields,
      { llm_model: "x", llm_temperature: "0", llm_api_key: "", max_total_runs: "20",
        cors_origins: '["http://a"]', llm_extra_body: "" },
      { llm_model: true },
    );
    assert.equal(body.llm_model, null);
  }

  {
    // Coercion errors block submission.
    const { body, errors } = buildUpdateBody(fields, {
      llm_model: "qwen", llm_temperature: "abc", llm_api_key: "", max_total_runs: "1.5",
      cors_origins: "not-json", llm_extra_body: "{oops",
    });
    assert.deepEqual(Object.keys(body), []);
    assert.equal(errors.llm_temperature, "必须是数字");
    assert.equal(errors.max_total_runs, "必须是整数");
    assert.equal(errors.cors_origins, "必须是 JSON 数组");
    assert.equal(errors.llm_extra_body, "必须是合法 JSON");
  }

  // --- settingsLabels: mapping coverage ---
  // The 65 DB-editable field names as of 2026-08 (mirrors courtier.config
  // SETTINGS_META; backend additions fall back to env_name gracefully).
  const BACKEND_FIELD_NAMES = [
    "loop_max_consecutive_exploratory", "loop_max_null_tool_results",
    "loop_max_reasoning_dupe_steps", "loop_max_same_tool_calls",
    "loop_max_turns_without_business_artifacts", "loop_reasoning_similarity_threshold",
    "max_runs_per_user", "max_total_runs", "run_grace_seconds", "run_log_max_bytes",
    "run_log_max_events", "subagent_max_cumulative_runtime_seconds", "subagent_max_depth",
    "subagent_max_runtime_seconds", "subagent_max_total_spawns", "subagent_max_turns",
    "context_budget_ratio", "context_compact_target_ratio", "context_max_user_message_chars",
    "context_micro_compact_ratio", "context_preview_max_chars",
    "context_recent_tool_results_tokens", "llm_api_key", "llm_base_url",
    "llm_context_window_tokens", "llm_embedding_batch_size", "llm_embedding_dim",
    "llm_embedding_model", "llm_extra_body", "llm_frequency_penalty", "llm_max_tokens",
    "llm_model", "llm_presence_penalty", "llm_temperature", "llm_timeout",
    "audit_log_enabled", "logger_level", "otel_exporter_otlp_endpoint", "otel_log_level",
    "otel_service_name", "courtier_plugin_endpoints", "courtier_plugin_token",
    "es_hosts", "es_index_chunks", "es_index_results", "es_password", "es_username",
    "minio_access_key", "minio_bucket_docs", "minio_bucket_documents",
    "minio_bucket_library", "minio_bucket_plugin_io", "minio_bucket_resources",
    "minio_endpoint", "minio_secret_key", "minio_secure",
    "search_rerank_candidate_budget_chars", "search_rerank_fetch",
    "bcrypt_rounds", "cors_allow_credentials", "cors_origins",
    "jwt_access_expire_seconds", "jwt_algorithm", "jwt_expire_seconds", "jwt_secret",
  ];
  {
    const missing = BACKEND_FIELD_NAMES.filter((name) => !(name in FIELD_DISPLAY_NAMES));
    assert.deepEqual(missing, [], `fields missing a Chinese label: ${missing.join(", ")}`);
  }

  // --- settingsLabels: display name + description cleanup ---
  assert.equal(fieldDisplayName({ name: "llm_api_key", env_name: "LLM_API_KEY" }), "API 密钥");
  assert.equal(fieldDisplayName({ name: "brand_new_field", env_name: "BRAND_NEW" }), "BRAND_NEW");
  assert.equal(
    cleanDescription("LLM API 端点 URL（环境变量: LLM_IP）"),
    "LLM API 端点 URL",
  );
  assert.equal(
    cleanDescription("Embedding 模型名（环境变量: LLM_EMBEDDING_MODEL）。空 = 向量检索关闭"),
    "Embedding 模型名。空 = 向量检索关闭",
  );

  // --- settingsLabels: sub-grouping ---
  const mkField = (name, category) => ({
    name, category, is_secret: false, effect: "hot", source: "default",
    value: null, type: "string", env_name: name.toUpperCase(), description: "",
  });
  {
    // 隐藏契约：端点三件套 + 全局采样默认值都在 HIDDEN_SETTING_FIELDS 中
    // （端点三件套对聊天链 dormant；采样默认值由服务端物化进每个池条目），
    // 设置页只剩 模型池 / Embedding / 上下文管理 三个可见组。
    const modelFields = [
      "llm_model_pool", "llm_endpoint_keys", "llm_model", "llm_api_key",
      "llm_temperature", "llm_timeout", "llm_embedding_model",
      "llm_embedding_dim", "context_budget_ratio", "context_preview_max_chars",
    ].map((name) => mkField(name, "model"));
    const groups = groupCategoryFields("model", modelFields);
    assert.deepEqual(
      groups.map((g) => g.key),
      ["pool", "embedding", "context"],
      "llm_embedding_ must win over the shorter llm_ prefix (first match)",
    );
    const total = groups.reduce((n, g) => n + g.fields.length, 0);
    assert.equal(
      total,
      6,
      "pool(2) + embedding(2) + context(2) visible; endpoint trio and scalar defaults hidden",
    );
    assert.equal(groups.find((g) => g.key === "pool").fields.length, 2);
  }
  {
    // Unknown category and unknown names both land in a trailing 其他 group.
    const groups = groupCategoryFields("unknown_cat", [mkField("x", "unknown_cat")]);
    assert.deepEqual(groups.map((g) => g.key), ["other"]);
    const mixed = groupCategoryFields("plugins", [
      mkField("courtier_plugin_token", "plugins"),
      mkField("some_future_field", "plugins"),
    ]);
    assert.deepEqual(mixed.map((g) => g.key), ["conn", "other"]);
  }
  {
    // guards: exact-name fields join the runs group alongside run_* prefixes.
    const groups = groupCategoryFields("guards", [
      mkField("loop_max_same_tool_calls", "guards"),
      mkField("max_runs_per_user", "guards"),
      mkField("run_grace_seconds", "guards"),
      mkField("subagent_max_depth", "guards"),
    ]);
    assert.deepEqual(groups.map((g) => g.key), ["loop", "subagent", "runs"]);
  }

  console.log("settings-form verification passed");
} finally {
  rmSync(outDir, { recursive: true, force: true });
}
