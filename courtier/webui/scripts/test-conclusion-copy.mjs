import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { rmSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const scriptDir = dirname(fileURLToPath(import.meta.url));
const rootDir = resolve(scriptDir, "..");
const outDir = resolve(rootDir, ".tmp/conclusion-copy-test");

rmSync(outDir, { recursive: true, force: true });
mkdirSync(outDir, { recursive: true });

execFileSync(
  resolve(rootDir, "node_modules/.bin/esbuild"),
  [
    resolve(rootDir, "src/utils/conclusionCopy.ts"),
    "--bundle",
    "--format=esm",
    "--platform=node",
    "--outfile=" + resolve(outDir, "conclusionCopy.js"),
  ],
  { cwd: rootDir, stdio: "inherit" },
);

const { buildConclusionCopy } = await import(
  pathToFileURL(resolve(outDir, "conclusionCopy.js")).href
);

// ---- 无引用：原文透传，html 走渲染管线 ----
{
  const content = "## 审核结论\n\n文档存在 **3 处**格式问题。";
  const { text, html } = buildConclusionCopy(content);
  assert.equal(text, content);
  assert.ok(html.includes("<h2>"), "heading rendered");
  assert.ok(html.includes("<strong>3 处</strong>"), "bold rendered");
}

// ---- 标记规范化：[[n]] → [n] ----
{
  const { text, html } = buildConclusionCopy("依据[[2]]与[[10]]核对。");
  assert.ok(text.includes("[2]"), "marker normalized to [2]");
  assert.ok(text.includes("[10]"), "multi-digit marker normalized");
  assert.ok(!text.includes("[[2]]"), "double-bracket marker gone");
  assert.equal(text, "依据[2]与[10]核对。");
}

// ---- 来源附录：按真实编号（跳号不重排）、带标题与文种 ----
{
  const hitA = { title: "国务院关于加强管理的通知", docType: "通知" };
  const hitB = { title: "地方标准编写规定", docType: "标准" };
  const citations = {
    byNumber: new Map([
      [1, hitA],
      [3, hitB],
    ]),
    list: [hitA, hitB],
  };
  const { text, html } = buildConclusionCopy("见[[1]]、[[3]]。", citations);
  assert.ok(text.includes("**参考来源**"));
  assert.ok(text.includes("- [1] 国务院关于加强管理的通知（通知）"));
  assert.ok(text.includes("- [3] 地方标准编写规定（标准）"));
  assert.ok(text.indexOf("[1]") < text.indexOf("[3]"), "ascending order");
  assert.ok(html.includes("参考来源") && html.includes("<li>"), "appendix rendered as list");
}

// ---- 附录编号保持跳号：仅有 [5]，不得重排为 [1] ----
{
  const citations = {
    byNumber: new Map([[5, { title: "某文件", docType: "" }]]),
    list: [{ title: "某文件", docType: "" }],
  };
  const { text } = buildConclusionCopy("见[[5]]。", citations);
  assert.ok(text.includes("- [5] 某文件"), "gap numbering preserved");
  assert.ok(!text.includes("（）"), "empty docType adds no empty parens");
}

// ---- 标题缺失回退：documentId/resourceId → 未命名文档；无文种不带括号 ----
{
  const citations = {
    byNumber: new Map([
      [2, { documentId: 7 }],
      [4, { resourceId: 9, title: "  " }],
      [6, {}],
    ]),
    list: [{ documentId: 7 }, { resourceId: 9, title: "  " }, {}],
  };
  const { text } = buildConclusionCopy("见[[2]]、[[4]]、[[6]]。", citations);
  assert.ok(text.includes("- [2] 文档 #7"), "documentId fallback");
  assert.ok(text.includes("- [4] 文档 #9"), "blank title falls back to resourceId");
  assert.ok(text.includes("- [6] 未命名文档"), "untitled fallback");
}

// ---- 无 citations / 空 list：不加附录 ----
{
  const a = buildConclusionCopy("结论正文");
  assert.ok(!a.text.includes("参考来源"));
  const b = buildConclusionCopy("结论正文", { byNumber: new Map(), list: [] });
  assert.ok(!b.text.includes("参考来源"));
  assert.equal(b.text, "结论正文");
}
