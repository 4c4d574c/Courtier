// Headless benchmark for the streaming hot path: buildChatMessages growth
// curve + streaming-markdown render cost.  Measurement harness for the
// webui streaming-perf plan (docs/architecture/webui-streaming-perf-plan.md).
// Not wired into `npm test`.  Run: node scripts/bench-streaming-perf.mjs
import { execFileSync } from "node:child_process";
import { rmSync, mkdirSync, readFileSync, writeFileSync, readdirSync, statSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const scriptDir = dirname(fileURLToPath(import.meta.url));
const rootDir = resolve(scriptDir, "..");
const outDir = resolve(rootDir, ".tmp/bench-streaming-perf");

function fixRelativeImports(dir) {
  for (const entry of readdirSync(dir)) {
    const fullPath = resolve(dir, entry);
    if (statSync(fullPath).isDirectory()) {
      fixRelativeImports(fullPath);
    } else if (fullPath.endsWith(".js")) {
      let source = readFileSync(fullPath, "utf8");
      source = source.replace(
        /from\s+["'](\.\/[^"']+?)(?!\.js)["']/g,
        'from "$1.js"',
      );
      source = source.replace(
        /from\s+["'](\.\.\/[^"']+?)(?!\.js)["']/g,
        'from "$1.js"',
      );
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
    "--target", "ES2020",
    "--module", "ES2020",
    "--moduleResolution", "bundler",
    "--strict",
    "--skipLibCheck",
    "--outDir", outDir,
    "--rootDir", resolve(rootDir, "src"),
    resolve(rootDir, "src/utils/chatMessages.ts"),
    resolve(rootDir, "src/utils/sessionUtils.ts"),
    resolve(rootDir, "src/utils/streamingMarkdown.ts"),
    resolve(rootDir, "src/constants/messages.ts"),
  ],
  { cwd: rootDir, stdio: "inherit" },
);
fixRelativeImports(outDir);

const { createRequire } = await import("node:module");
const require = createRequire(import.meta.url);
const { reactive } = require(
  resolve(rootDir, "node_modules/vue/index.js"),
);
const { buildChatMessages } = await import(
  pathToFileURL(resolve(outDir, "utils/chatMessages.js")).href
);
const { thoughtsForStep } = await import(
  pathToFileURL(resolve(outDir, "utils/sessionUtils.js")).href
);
const { createStreamingRenderer } = await import(
  pathToFileURL(resolve(outDir, "utils/streamingMarkdown.js")).href
);

// ---- fixtures (shapes mirror sessionEventHandlers/state.ts writes) ----

const THINK_TEXT = "用户要求审核这份公文，我需要先解析文档结构，再逐条对照 GB/T 9704 检查版记与标题格式。";
const OBSERVE_TEXT = "工具返回了解析结果，标题层级完整，接下来检查发文字号与成文日期的编排位置。";

function makeThought(id, stepIndex, text, segmentIndex) {
  return {
    id,
    text,
    turn: stepIndex + 1,
    turnIndex: 1,
    segmentIndex,
    segmentType: segmentIndex % 2 === 0 ? "observe" : undefined,
    stepIndex,
    timestamp: id,
  };
}

function makeTool(id, index, status) {
  return {
    id,
    name: index % 3 === 0 ? "search_documents" : "check_format",
    skill: index % 3 === 0 ? "search" : "format_audit",
    status,
    callKind: "tool",
    callScope: "parent",
    subagentName: null,
    summary: "检查完成，未发现问题",
  };
}

function makeSession(numSteps, thoughtsPerStep) {
  const steps = [];
  const thoughts = [];
  let thoughtId = 0;
  for (let i = 0; i < numSteps; i++) {
    steps.push({
      index: i,
      numeral: String(i + 1),
      label: "check_format, parse_document",
      skill: "format_audit",
      tools: [
        makeTool(`tool-${i}a`, i, "done"),
        makeTool(`tool-${i}b`, i + 1, i === numSteps - 1 ? "running" : "done"),
      ],
      verdict: i % 4 === 3 ? "中间结论：格式检查未发现明显偏差，继续内容审查。" : undefined,
      turnIndex: 1,
      startSegmentIndex: i * 2,
      endSegmentIndex: i * 2 + 2,
    });
    for (let k = 0; k < thoughtsPerStep; k++) {
      thoughts.push(
        makeThought(++thoughtId, i, k % 2 === 0 ? THINK_TEXT : OBSERVE_TEXT, i * 2 + k),
      );
    }
  }
  return {
    id: "bench",
    task: "基准会话",
    modelName: "bench",
    status: "running",
    turns: [
      {
        message: { role: "user", text: "审核这份文件", timestamp: 1 },
        steps,
      },
    ],
    steps,
    thoughts,
    stats: { tokensIn: 0, tokensOut: 0, elapsed: 0 },
    createdAt: 1,
  };
}

function bench(fn, iterations) {
  fn(); // warmup + JIT
  const t0 = process.hrtime.bigint();
  for (let i = 0; i < iterations; i++) fn();
  const t1 = process.hrtime.bigint();
  return Number(t1 - t0) / 1e6 / iterations; // ms per op
}

console.log("== A. buildChatMessages per-call cost vs session size ==");
console.log("   (one streaming token = replace last thought object, then rebuild)");
console.log("steps | thoughts | items | raw ms/call | reactive ms/call");
for (const numSteps of [5, 10, 20, 40, 80]) {
  const thoughtsPerStep = 2;
  const session = makeSession(numSteps, thoughtsPerStep);
  const items = buildChatMessages(session, []);
  const rSession = reactive(structuredClone(session));
  const last = rSession.thoughts[rSession.thoughts.length - 1];
  rSession.thoughts[rSession.thoughts.length - 1] = { ...last, text: last.text + "字" };
  const rawMs = bench(() => buildChatMessages(session, []), 200);
  const reactiveMs = bench(() => buildChatMessages(rSession, []), 200);
  console.log(
    `${String(numSteps).padStart(5)} | ${String(session.thoughts.length).padStart(8)} | ${String(items.length).padStart(5)} | ${rawMs.toFixed(3).padStart(11)} | ${reactiveMs.toFixed(3).padStart(11)}`,
  );
}

console.log("== A2. rebuild cost with frozen history (1 done turn + streaming turn) ==");
console.log("     history turn = N steps; streaming turn = 1 step growing thoughts");
console.log("history steps | raw ms/call | reactive ms/call");
for (const numSteps of [20, 40, 80]) {
  const doneTurn = makeSession(numSteps, 2).turns[0];
  doneTurn.conclusion = "历史轮结论。";
  const live = makeSession(1, 2).turns[0];
  const session = makeSession(1, 2);
  session.status = "running";
  session.turns = [doneTurn, live];
  session.steps = [...doneTurn.steps, ...live.steps];
  // tokens stream into the LIVE turn: replace its last thought each time
  const last = live.steps[live.steps.length - 1];
  const rSession = reactive(structuredClone({ ...session }));
  const rawLast = session.thoughts[session.thoughts.length - 1];
  session.thoughts[session.thoughts.length - 1] = { ...rawLast, text: rawLast.text + "字" };
  const rLast = rSession.thoughts[rSession.thoughts.length - 1];
  rSession.thoughts[rSession.thoughts.length - 1] = { ...rLast, text: rLast.text + "字" };
  buildChatMessages(session, []);
  buildChatMessages(rSession, []); // warm caches
  const rawMs = bench(() => buildChatMessages(session, []), 200);
  const reactiveMs = bench(() => buildChatMessages(rSession, []), 200);
  console.log(`${String(numSteps).padStart(13)} | ${rawMs.toFixed(3).padStart(11)} | ${reactiveMs.toFixed(3).padStart(11)}`);
}

console.log("\n== B. thoughtsForStep alone (per call) ==");
console.log("steps | thoughts | µs/call");
for (const numSteps of [10, 40, 80]) {
  const session = makeSession(numSteps, 2);
  const turn = session.turns[0];
  const us = bench(() => thoughtsForStep(session.thoughts, turn, numSteps - 1), 500) * 1000;
  console.log(`${String(numSteps).padStart(5)} | ${String(session.thoughts.length).padStart(8)} | ${us.toFixed(1)}`);
}

console.log("\n== C. renderStreamingHtml per-update cost vs conclusion length ==");
console.log("     C1: plain-text append (trailing-span fast path)");
console.log("     C2: structural char arriving (full re-lex path)");
for (const targetLen of [1000, 4000, 16000, 40000]) {
  // C1 fast path: grow plain CJK text, then time incremental appends
  {
    const r = createStreamingRenderer();
    let text = "";
    let i = 0;
    while (text.length < targetLen) {
      text += "文";
      if (++i % 25 === 0) text += "，";
      r.renderStreamingHtml(text, true);
    }
    const samples = [];
    for (let k = 0; k < 20; k++) {
      const next = text + "字";
      const t0 = process.hrtime.bigint();
      r.renderStreamingHtml(next, true);
      samples.push(Number(process.hrtime.bigint() - t0) / 1e6);
      text = next;
    }
    samples.sort((a, b) => a - b);
    console.log(`len=${String(text.length).padStart(6)}  C1 fast-path median: ${samples[10].toFixed(4)} ms/update`);
  }
  // C2 structural path: newline arrives every ~12 chunks (full re-lex)
  {
    const r = createStreamingRenderer();
    let text = "";
    let k = 0;
    const chunk = () => (++k % 12 === 0 ? "\n\n" : "这是一段中文内容，");
    while (text.length < targetLen) {
      text += chunk();
      r.renderStreamingHtml(text, true);
    }
    const samples = [];
    for (let j = 0; j < 10; j++) {
      const next = text + chunk();
      const t0 = process.hrtime.bigint();
      r.renderStreamingHtml(next, true);
      samples.push(Number(process.hrtime.bigint() - t0) / 1e6);
      text = next;
    }
    samples.sort((a, b) => a - b);
    console.log(`len=${String(text.length).padStart(6)}  C2 re-lex median:     ${samples[5].toFixed(4)} ms/update`);
  }
}
