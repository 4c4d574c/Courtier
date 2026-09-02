import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { rmSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const scriptDir = dirname(fileURLToPath(import.meta.url));
const rootDir = resolve(scriptDir, "..");
const outDir = resolve(rootDir, ".tmp/refusal-events-test");

rmSync(outDir, { recursive: true, force: true });
mkdirSync(outDir, { recursive: true });

execFileSync(
  resolve(rootDir, "node_modules/.bin/esbuild"),
  [
    resolve(rootDir, "src/composables/sessionEventHandlers/index.ts"),
    "--bundle",
    "--format=esm",
    "--platform=node",
    "--outfile=" + resolve(outDir, "sessionEventHandlers.bundle.js"),
  ],
  { cwd: rootDir, stdio: "inherit" },
);

const { createSessionEventHandlers } = await import(
  pathToFileURL(resolve(outDir, "sessionEventHandlers.bundle.js")).href
);

function makeDeps() {
  const session = {
    id: "sess_1",
    task: "t",
    modelName: "",
    status: "running",
    turns: [],
    steps: [],
    thoughts: [],
    stats: { tokensIn: 0, tokensOut: 0, elapsed: 0 },
    createdAt: Date.now(),
    pendingVerdict: "",
  };
  const state = {
    currentTurn: null,
    currentTurnIndex: 0,
    currentStepIndex: 1,
    segmentIndex: 0,
    currentSegmentType: "think",
    currentThoughtTurn: 2,
    observedSinceLastStep: false,
    thoughtIdCounter: 3,
    toolIdCounter: 0,
    subagentThoughtCounters: {},
    pendingSubagents: [],
  };
  const handlers = createSessionEventHandlers(() => ({ session, state }));
  return { session, state, handlers };
}

// think_retry: drop the current think's streamed thought + verdict buffer
{
  const { session, state, handlers } = makeDeps();
  session.thoughts.push({
    id: 3,
    text: "被拒尝试的推理",
    turn: 2,
    turnIndex: 0,
    segmentIndex: 0,
    segmentType: "think",
    stepIndex: 1,
    timestamp: Date.now(),
  });
  session.pendingVerdict = "被拒尝试的结论";
  handlers.handleSessionEvent({ type: "think_retry", attempt: 1 });
  assert.equal(session.thoughts.length, 0, "refused attempt's thought is dropped");
  assert.equal(session.pendingVerdict, "");

  // retry re-streams into a fresh thought (same machinery as first pass)
  session.thoughts.push({
    id: 4,
    text: "重试推理",
    turn: 2,
    turnIndex: 0,
    segmentIndex: 0,
    segmentType: "think",
    stepIndex: 1,
    timestamp: Date.now(),
  });
  assert.equal(session.thoughts[0].text, "重试推理");
  assert.equal(state.currentThoughtTurn, 2);
}

// think_retry keeps history thoughts (turn mismatch)
{
  const { session, handlers } = makeDeps();
  session.thoughts.push({
    id: 1,
    text: "历史 thought",
    turn: 1,
    turnIndex: 0,
    segmentIndex: 0,
    segmentType: "think",
    timestamp: Date.now(),
  });
  handlers.handleSessionEvent({ type: "think_retry", attempt: 1 });
  assert.equal(session.thoughts.length, 1, "history thought untouched");
}

// refusal_exhausted: banner text set (locale-rendered by the backend)
{
  const { session, handlers } = makeDeps();
  handlers.handleSessionEvent({
    type: "refusal_exhausted",
    text: "模型多次拒绝执行该任务，当前模型可能不可用。",
    matched: "我无法",
  });
  assert.equal(session.refusalNotice, "模型多次拒绝执行该任务，当前模型可能不可用。");
}

console.log("refusal event handler tests passed");
