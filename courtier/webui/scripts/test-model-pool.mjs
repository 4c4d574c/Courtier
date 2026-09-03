import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { rmSync, mkdirSync, readFileSync, writeFileSync, readdirSync, statSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const scriptDir = dirname(fileURLToPath(import.meta.url));
const rootDir = resolve(scriptDir, "..");
const outDir = resolve(rootDir, ".tmp/model-pool-test");

function fixRelativeImports(dir) {
  for (const entry of readdirSync(dir)) {
    const fullPath = resolve(dir, entry);
    if (statSync(fullPath).isDirectory()) {
      fixRelativeImports(fullPath);
    } else if (fullPath.endsWith(".js")) {
      let source = readFileSync(fullPath, "utf8");
      source = source.replace(/from\s+["'](\.\/[^"']+?)(?!\.js)["']/g, 'from "$1.js"');
      writeFileSync(fullPath, source);
    }
  }
}

rmSync(outDir, { recursive: true, force: true });
mkdirSync(outDir, { recursive: true });

execFileSync(
  process.execPath,
  [
    resolve(rootDir, "node_modules/typescript/bin/tsc"),
    "--target", "es2022", "--module", "esnext", "--moduleResolution", "bundler",
    "--strict", "--noEmitOnError", "--outDir", outDir,
    resolve(rootDir, "src/utils/modelPool.ts"),
  ],
  { stdio: "inherit" },
);
fixRelativeImports(outDir);

const {
  parsePool,
  serializeEndpoint,
  serializePool,
  buildKeyOps,
  keyOpsDirty,
  endpointChanged,
  poolErrors,
  emptyEndpointRow,
  emptyModelRow,
  genId,
} = await import(pathToFileURL(resolve(outDir, "modelPool.js")).href);

const SAVED_DOC = {
  endpoints: [
    {
      id: "ep_a",
      name: "接入点A",
      base_url: "https://a.example.com/v1",
      enabled: true,
      models: [
        { id: "mdl_a1", name: "模型A1", model: "model-a1" },
        {
          id: "mdl_a2",
          name: "模型A2",
          model: "model-a2",
          context_window_tokens: 65536,
          temperature: 0.5,
          timeout_seconds: 120,
          extra_body: { top_p: 0.9 },
        },
      ],
    },
  ],
  default_model_id: "mdl_a1",
};
const KEYS_VIEW = { ep_a: { set: true, tail: "sk-1" } };

// --- parsePool ---
{
  const { rows, defaultModelId } = parsePool(SAVED_DOC, KEYS_VIEW);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].id, "ep_a");
  assert.equal(rows[0].keySet, true);
  assert.equal(rows[0].keyTail, "sk-1");
  assert.equal(rows[0].keyDraft, "");
  assert.equal(rows[0].models[1].context_window_tokens, "65536");
  assert.equal(rows[0].models[1].temperature, "0.5");
  assert.equal(rows[0].models[1].timeout_seconds, "120");
  assert.equal(rows[0].models[1].extra_body, '{"top_p":0.9}');
  assert.equal(defaultModelId, "mdl_a1");
}
{
  const { rows, defaultModelId } = parsePool(null, null);
  assert.deepEqual(rows, []);
  assert.equal(defaultModelId, "");
}

// --- round-trip: parse → serialize without edits == original doc ---
{
  const { rows, defaultModelId } = parsePool(SAVED_DOC, KEYS_VIEW);
  const reparsed = JSON.parse(serializePool(rows, defaultModelId));
  assert.deepEqual(reparsed, SAVED_DOC);
}

// --- advanced fields: unset stays unset; typing a value round-trips ---
{
  const { rows, defaultModelId } = parsePool(SAVED_DOC, KEYS_VIEW);
  const entry = JSON.parse(serializeEndpoint(rows[0]));
  assert.ok(!("timeout_seconds" in entry.models[0]));
  assert.ok(!("extra_body" in entry.models[0]));

  rows[0].models[0].frequency_penalty = "0.5";
  rows[0].models[0].presence_penalty = "-0.3";
  rows[0].models[0].extra_body = '{ "top_k": 40 }';
  const edited = JSON.parse(serializeEndpoint(rows[0]));
  assert.equal(edited.models[0].frequency_penalty, 0.5);
  assert.equal(edited.models[0].presence_penalty, -0.3);
  assert.deepEqual(edited.models[0].extra_body, { top_k: 40 });

  // invalid extra_body is omitted from serialization (poolErrors reports it)
  rows[0].models[0].extra_body = "not-json";
  const invalid = JSON.parse(serializeEndpoint(rows[0]));
  assert.ok(!("extra_body" in invalid.models[0]));
  assert.ok(poolErrors(rows, "mdl_a1").includes("额外请求体"));
}

// --- buildKeyOps / keyOpsDirty ---
{
  const { rows } = parsePool(SAVED_DOC, KEYS_VIEW);
  assert.deepEqual(buildKeyOps(rows), {});
  assert.equal(keyOpsDirty(rows), false);

  rows[0].keyDraft = "  sk-new  ";
  assert.deepEqual(buildKeyOps(rows), { ep_a: "sk-new" });
  assert.equal(keyOpsDirty(rows), true);

  rows[0].keyDraft = "";
  rows[0].keyRemove = true;
  assert.deepEqual(buildKeyOps(rows), { ep_a: null });

  // draft wins over removal (row shows a typed value)
  rows[0].keyDraft = "sk-again";
  assert.deepEqual(buildKeyOps(rows), { ep_a: "sk-again" });
}

// --- endpointChanged ---
{
  const { rows } = parsePool(SAVED_DOC, KEYS_VIEW);
  assert.equal(endpointChanged(rows[0]), false);

  rows[0].name = "改名";
  assert.equal(endpointChanged(rows[0]), true);

  const fresh = emptyEndpointRow();
  assert.equal(endpointChanged(fresh), true); // new row counts as changed

  const untouched = parsePool(SAVED_DOC, KEYS_VIEW).rows[0];
  untouched.models[1].max_tokens = "2048";
  assert.equal(endpointChanged(untouched), true);
}

// --- poolErrors ---
{
  const good = parsePool(SAVED_DOC, KEYS_VIEW);
  assert.equal(poolErrors(good.rows, good.defaultModelId), "");

  assert.equal(poolErrors([], ""), ""); // 空池合法（回退标量）
  const oneModel = parsePool(SAVED_DOC, KEYS_VIEW);
  oneModel.defaultModelId = "";
  assert.ok(poolErrors(oneModel.rows, "").includes("请选择默认模型"));

  const dupEp = parsePool(SAVED_DOC, KEYS_VIEW);
  dupEp.rows.push({ ...JSON.parse(JSON.stringify(dupEp.rows[0])), id: "ep_a", savedSnapshot: "x" });
  assert.ok(poolErrors(dupEp.rows, "mdl_a1").includes("接入点 id 重复"));

  const dupModel = parsePool(SAVED_DOC, KEYS_VIEW);
  dupModel.rows[0].models.push(emptyModelRow());
  dupModel.rows[0].models[2].id = "mdl_a1";
  dupModel.rows[0].models[2].name = "x";
  dupModel.rows[0].models[2].model = "x";
  assert.ok(poolErrors(dupModel.rows, "mdl_a1").includes("模型 id 重复"));

  const missingName = parsePool(SAVED_DOC, KEYS_VIEW);
  missingName.rows[0].name = " ";
  assert.ok(poolErrors(missingName.rows, "mdl_a1").includes("接入点名称不能为空"));

  const badDefault = parsePool(SAVED_DOC, KEYS_VIEW);
  assert.ok(poolErrors(badDefault.rows, "mdl_ghost").includes("默认模型不存在"));

  const badNumber = parsePool(SAVED_DOC, KEYS_VIEW);
  badNumber.rows[0].models[1].context_window_tokens = "abc";
  assert.ok(poolErrors(badNumber.rows, "mdl_a1").includes("必须是数字"));

  const badTimeout = parsePool(SAVED_DOC, KEYS_VIEW);
  badTimeout.rows[0].models[1].timeout_seconds = "0";
  assert.ok(poolErrors(badTimeout.rows, "mdl_a1").includes("超时必须大于 0"));

  const badPenalty = parsePool(SAVED_DOC, KEYS_VIEW);
  badPenalty.rows[0].models[1].frequency_penalty = "3";
  assert.ok(poolErrors(badPenalty.rows, "mdl_a1").includes("惩罚系数"));

  const badTemp = parsePool(SAVED_DOC, KEYS_VIEW);
  badTemp.rows[0].models[1].temperature = "2.5";
  assert.ok(poolErrors(badTemp.rows, "mdl_a1").includes("温度需在 0 ~ 2"));
}

// --- modalities round-trip ---
{
  const doc = {
    endpoints: [
      {
        id: "ep_m",
        name: "多模态接入点",
        base_url: "https://m.example.com/v1",
        enabled: true,
        models: [
          { id: "mdl_v", name: "视觉模型", model: "vl-model", modalities: ["vision", "video"] },
          { id: "mdl_t", name: "文本模型", model: "text-model" },
        ],
      },
    ],
    default_model_id: "mdl_v",
  };
  const { rows } = parsePool(doc, null);
  assert.deepEqual(rows[0].models[0].modalities, ["vision", "video"]);
  // Legacy entries without the field default to [] (text-only).
  assert.deepEqual(rows[0].models[1].modalities, []);

  // Serialize: declared modalities persist, empty ones are omitted.
  const serialized = JSON.parse(serializeEndpoint(rows[0]));
  assert.deepEqual(serialized.models[0].modalities, ["vision", "video"]);
  assert.ok(!("modalities" in serialized.models[1]));

  // Untouched round-trip: parse → serialize → parse keeps the same modalities
  // without marking the row dirty.
  const again = parsePool(doc, null);
  assert.equal(serializeEndpoint(again.rows[0]), serializeEndpoint(rows[0]));

  // toggleModality semantics exercised through the row directly (the Vue
  // handler mirrors this): add then remove.
  const row = rows[0];
  row.models[1].modalities = ["audio"];
  const withAudio = JSON.parse(serializeEndpoint(row));
  assert.deepEqual(withAudio.models[1].modalities, ["audio"]);

  // Malformed modalities from hand-edited JSON are filtered, not crashing.
  const bad = parsePool(
    { endpoints: [{ ...doc.endpoints[0], models: [{ id: "x", name: "x", model: "x", modalities: "vision" }] }] },
    null,
  );
  assert.deepEqual(bad.rows[0].models[0].modalities, []);
}

// --- genId / empty rows ---
{
  assert.match(genId("ep"), /^ep_[0-9a-f]{8}$/);
  assert.match(genId("mdl"), /^mdl_[0-9a-f]{8}$/);
  const row = emptyEndpointRow();
  assert.equal(row.enabled, true);
  assert.deepEqual(row.models, []);
  const model = emptyModelRow();
  assert.match(model.id, /^mdl_/);
}

console.log("modelPool tests passed");
