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

  const { initFormState, buildUpdateBody, secretPlaceholder } = await import(
    pathToFileURL(resolve(outDir, "settingsForm.js")).href
  );

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

  console.log("settings-form verification passed");
} finally {
  rmSync(outDir, { recursive: true, force: true });
}
