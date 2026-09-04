import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { rmSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const scriptDir = dirname(fileURLToPath(import.meta.url));
const rootDir = resolve(scriptDir, "..");
const outDir = resolve(rootDir, ".tmp/guard-rows-test");

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
  parseGuardRows,
  serializeGuardRows,
  guardRowError,
  parseStringList,
  serializeStringList,
  DEFAULT_GUARD_ROWS,
} = await import(
  pathToFileURL(resolve(outDir, "settingsForm.bundle.js")).href
);

// —— parse：设置 JSON 文本 → 守卫声明行 ——

const parsed = parseGuardRows(
  JSON.stringify([
    {
      name: "tool_disabled",
      class_path: "a.b.ToolDisabledGuard",
      scope: "session",
      enabled: true,
      builtin: true,
    },
    { name: "my_guard", class_path: "x.y.MyGuard", scope: "run" },
  ])
);
assert.equal(parsed.length, 2);
assert.deepEqual(
  parsed[0],
  {
    name: "tool_disabled",
    classPath: "a.b.ToolDisabledGuard",
    scope: "session",
    enabled: true,
    builtin: true,
  },
  "builtin 行完整解析"
);
assert.equal(parsed[1].scope, "run", "run 作用域解析");
assert.equal(parsed[1].enabled, true, "enabled 缺省为 true");
assert.equal(parsed[1].builtin, false, "builtin 缺省为 false");

// 非法/缺省输入容错
assert.deepEqual(parseGuardRows(""), [], "空文本 = 空行表");
assert.deepEqual(parseGuardRows("not-json"), [], "坏 JSON = 空行表");
assert.deepEqual(parseGuardRows('{"a":1}'), [], "非数组 = 空行表");
const [loose] = parseGuardRows(JSON.stringify([{ name: 42, extra: true }]));
assert.equal(loose.name, "", "非字符串名称归一为空");
assert.equal(loose.classPath, "", "缺失类路径归一为空");

// —— serialize：行 → 设置 JSON 文本 ——

const serialized = serializeGuardRows([
  { name: "g1", classPath: "x.Y", scope: "run", enabled: false, builtin: false },
  { name: "", classPath: "x.Z", scope: "session", enabled: true, builtin: false },
]);
assert.deepEqual(
  JSON.parse(serialized),
  [{ name: "g1", class_path: "x.Y", scope: "run", enabled: false, builtin: false }],
  "半行（缺名称）被剔除，字段名回到 snake_case"
);

// 往返一致
const roundTrip = parseGuardRows(serializeGuardRows(parsed));
assert.deepEqual(roundTrip, parsed, "parse ∘ serialize 恒等");

// —— 行校验 ——

assert.equal(guardRowError({ name: "g", classPath: "x.Y", scope: "session", enabled: true, builtin: false }), "");
assert.equal(guardRowError({ name: "", classPath: "", scope: "session", enabled: true, builtin: false }), "", "全空行不算半行");
assert.equal(guardRowError({ name: "g", classPath: "", scope: "session", enabled: true, builtin: false }), "缺少类路径");
assert.equal(guardRowError({ name: "", classPath: "x.Y", scope: "session", enabled: true, builtin: false }), "缺少守卫名称");

// —— 默认基线 ——

assert.equal(DEFAULT_GUARD_ROWS.length, 5);
assert.deepEqual(
  DEFAULT_GUARD_ROWS.map((row) => row.name),
  ["tool_disabled", "path_policy", "confirmation", "explore_loop", "business_artifact"],
  "默认基线 = 五个内置守卫，顺序与派生顺序一致"
);
assert.ok(DEFAULT_GUARD_ROWS.every((row) => row.builtin && row.enabled));

// —— 字符串名单（tools_disabled）——

assert.deepEqual(parseStringList('["deploy", " deploy "]'), ["deploy", " deploy "], "原文返回，去重在序列化端");
assert.deepEqual(parseStringList(""), []);
assert.deepEqual(parseStringList("bad"), []);
assert.deepEqual(parseStringList('{"a":1}'), []);

assert.equal(serializeStringList(["deploy", " deploy ", "", "deploy", "read"]), '["deploy","read"]', "去空白 + 保序去重");
assert.equal(serializeStringList([]), "[]");

console.log("test-guard-rows: all assertions passed");
