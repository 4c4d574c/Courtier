import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { rmSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const scriptDir = dirname(fileURLToPath(import.meta.url));
const rootDir = resolve(scriptDir, "..");
const outDir = resolve(rootDir, ".tmp/tool-path-rows-test");

rmSync(outDir, { recursive: true, force: true });
mkdirSync(outDir, { recursive: true });

execFileSync(
  resolve(rootDir, "node_modules/.bin/esbuild"),
  [
    resolve(rootDir, "src/utils/settingsForm.ts"),
    "--bundle",
    "--format=esm",
    "--platform=node",
    "--outfile=" + resolve(outDir, "settingsForm.bundle.js"),
  ],
  { cwd: rootDir, stdio: "inherit" },
);

const {
  parseToolPathRows,
  serializeToolPathRows,
  toolPathRowError,
  parseToolConfirmationRows,
  serializeToolConfirmationRows,
  toolConfirmationRowError,
} = await import(
  pathToFileURL(resolve(outDir, "settingsForm.bundle.js")).href
);

// parse：设置 JSON 文本 → 行（路径为逐条数组）
const rows = parseToolPathRows(
  JSON.stringify({ export_report: ["/srv/reports"], write: false })
);
assert.deepEqual(rows, [
  { tool: "export_report", paths: ["/srv/reports"], exempt: false },
  { tool: "write", paths: [], exempt: true },
]);

// 序列化往返（豁免行忽略路径；空工具名跳过；路径逐条去空白）
const serialized = serializeToolPathRows([
  { tool: "a", paths: ["/x", " /y "], exempt: false },
  { tool: "b", paths: [], exempt: true },
  { tool: "  ", paths: ["/z"], exempt: false },
]);
assert.deepEqual(JSON.parse(serialized), { a: ["/x", "/y"], b: false });

// 半行校验
assert.equal(
  toolPathRowError({ tool: "t", exempt: false, paths: [] }),
  "需要至少一个路径，或勾选豁免",
);
assert.equal(toolPathRowError({ tool: "", exempt: false, paths: ["/x"] }), "缺少工具名");
assert.equal(toolPathRowError({ tool: "t", exempt: true, paths: ["/x"] }), "");

// 坏 JSON → 空行（编辑器显示空表，保存时被序列化兜底）
assert.deepEqual(parseToolPathRows("not-json"), []);

console.log("tool path rows tests passed");

// 确认名单行：解析 / 序列化 / 半行校验
const confirmRows = parseToolConfirmationRows(
  JSON.stringify([
    { tool: "write", message: "写入需要确认" },
    { tool: "deploy", message: "" },
  ])
);
assert.deepEqual(confirmRows, [
  { tool: "write", message: "写入需要确认" },
  { tool: "deploy", message: "" },
]);
const out = JSON.parse(serializeToolConfirmationRows(confirmRows));
assert.deepEqual(out, [
  { tool: "write", message: "写入需要确认" },
  { tool: "deploy" },
]);
assert.equal(toolConfirmationRowError({ tool: "", message: "hi" }), "缺少工具名");
assert.equal(toolConfirmationRowError({ tool: "w", message: "" }), "");

// 坏 JSON / 非数组 → 空行
assert.deepEqual(parseToolConfirmationRows("not-json"), []);
assert.deepEqual(parseToolConfirmationRows("{}"), []);
console.log("confirmation rows tests passed");
