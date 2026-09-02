import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { rmSync, mkdirSync, readFileSync, writeFileSync, readdirSync, statSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const scriptDir = dirname(fileURLToPath(import.meta.url));
const rootDir = resolve(scriptDir, "..");
const outDir = resolve(rootDir, ".tmp/model-selection-test");

function fixRelativeImports(dir) {
  for (const entry of readdirSync(dir)) {
    const fullPath = resolve(dir, entry);
    if (statSync(fullPath).isDirectory()) {
      fixRelativeImports(fullPath);
    } else if (fullPath.endsWith(".js")) {
      let source = readFileSync(fullPath, "utf8");
      // Idempotent: already-suffixed specifiers are left alone (the script
      // runs once per compile over the same outDir).
      source = source.replace(
        /(from\s+["']\.[^"']*?)["']/g,
        (m, spec) => (spec.endsWith(".js") ? m : `${spec}.js"`),
      );
      writeFileSync(fullPath, source);
    }
  }
}

rmSync(outDir, { recursive: true, force: true });
mkdirSync(outDir, { recursive: true });

function compile(entry) {
  execFileSync(
    process.execPath,
    [
      resolve(rootDir, "node_modules/typescript/bin/tsc"),
      "--target", "es2022", "--module", "esnext", "--moduleResolution", "bundler",
      "--strict", "--noEmitOnError", "--outDir", outDir,
      resolve(rootDir, entry),
    ],
    { stdio: "inherit" },
  );
}

compile("src/composables/useModelPool.ts");
fixRelativeImports(outDir);
compile("src/composables/sessionEventHandlers/handlers/runtime.ts");
fixRelativeImports(outDir);

const { api } = await import(pathToFileURL(resolve(outDir, "api/client.js")).href);
const { useModelPool } = await import(
  pathToFileURL(resolve(outDir, "composables/useModelPool.js")).href
);
const { handleRuntimeEvent } = await import(
  pathToFileURL(resolve(outDir, "composables/sessionEventHandlers/handlers/runtime.js")).href
);

const POOL = {
  endpoints: [
    {
      endpointId: "ep_a",
      endpointName: "接入点A",
      models: [
        { id: "mdl_a1", name: "模型A1" },
        { id: "mdl_a2", name: "模型A2" },
      ],
    },
    {
      endpointId: "ep_b",
      endpointName: "接入点B",
      models: [{ id: "mdl_b1", name: "模型B1" }],
    },
  ],
  defaultModelId: "mdl_a1",
};

// --- loadModels: populates groups + preselects the default ---
{
  api.getModels = async () => JSON.parse(JSON.stringify(POOL));
  const pool = useModelPool();
  await pool.loadModels();
  assert.equal(pool.loaded.value, true);
  assert.equal(pool.endpoints.value.length, 2);
  assert.equal(pool.selectedModelId.value, "mdl_a1");
  assert.equal(pool.currentModelName(), "模型A1");
}

// --- selectModel + modelName ---
{
  const pool = useModelPool();
  pool.selectModel("mdl_b1");
  assert.equal(pool.selectedModelId.value, "mdl_b1");
  assert.equal(pool.currentModelName(), "模型B1");
}

// --- preselectModel: session provenance; unknown ids are ignored ---
{
  const pool = useModelPool();
  pool.preselectModel("mdl_a2");
  assert.equal(pool.selectedModelId.value, "mdl_a2");
  pool.preselectModel("mdl_ghost");
  assert.equal(pool.selectedModelId.value, "mdl_a2"); // unchanged
}

// --- refresh: removed selection falls back to the pool default ---
{
  const shrunk = JSON.parse(JSON.stringify(POOL));
  shrunk.endpoints = [
    {
      endpointId: "ep_a",
      endpointName: "接入点A",
      models: [{ id: "mdl_a1", name: "模型A1" }],
    },
  ];
  shrunk.defaultModelId = "mdl_a1";
  api.getModels = async () => shrunk;
  const pool = useModelPool();
  await pool.loadModels(true);
  assert.equal(pool.selectedModelId.value, "mdl_a1");
  assert.equal(pool.currentModelName(), "模型A1");
}

// --- empty pool: modelName stays "" (InputArea falls back to the scalar name) ---
{
  api.getModels = async () => ({ endpoints: [], defaultModelId: "" });
  const pool = useModelPool();
  await pool.loadModels(true);
  assert.equal(pool.endpoints.value.length, 0);
  assert.equal(pool.modelName.value, "");
}

// --- runtime handler: model_selected updates the session display ---
{
  const session = { modelName: "", lastModelId: "" };
  handleRuntimeEvent(
    () => ({ session }),
    { type: "model_selected", model: "模型B1", modelId: "mdl_b1", backend: "pool" },
  );
  assert.equal(session.modelName, "模型B1");
  assert.equal(session.lastModelId, "mdl_b1");

  handleRuntimeEvent(
    () => ({ session }),
    { type: "model_selected", model: "旧模型", backend: "scalar" },
  );
  assert.equal(session.lastModelId, "");
}

console.log("model selection tests passed");
