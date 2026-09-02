import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { rmSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const scriptDir = dirname(fileURLToPath(import.meta.url));
const rootDir = resolve(scriptDir, "..");
const outDir = resolve(rootDir, ".tmp/confirmation-events-test");

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
    pendingConfirmations: [],
  };
  const state = {
    currentTurn: null,
    currentTurnIndex: 0,
    currentStepIndex: 0,
    segmentIndex: 0,
    currentSegmentType: "observe",
    currentThoughtTurn: 0,
    observedSinceLastStep: false,
    thoughtIdCounter: 0,
    toolIdCounter: 0,
    subagentThoughtCounters: {},
    pendingSubagents: [],
  };
  const handlers = createSessionEventHandlers(() => ({ session, state }));
  return { session, handlers };
}

// requested → pending entry appended
{
  const { session, handlers } = makeDeps();
  handlers.handleSessionEvent({
    type: "confirmation_requested",
    confirmationId: "cf_1",
    toolName: "deploy",
    message: "确认部署？",
  });
  assert.equal(session.pendingConfirmations.length, 1);
  assert.equal(session.pendingConfirmations[0].toolName, "deploy");
  assert.equal(session.pendingConfirmations[0].message, "确认部署？");

  // second request accumulates (same-batch serial asks)
  handlers.handleSessionEvent({
    type: "confirmation_requested",
    confirmationId: "cf_2",
    toolName: "purge",
    message: "",
  });
  assert.equal(session.pendingConfirmations.length, 2);

  // resolved → card removed (idempotent)
  handlers.handleSessionEvent({
    type: "confirmation_resolved",
    confirmationId: "cf_1",
    decision: "approve",
  });
  assert.equal(session.pendingConfirmations.length, 1);
  assert.equal(session.pendingConfirmations[0].confirmationId, "cf_2");
  handlers.handleSessionEvent({
    type: "confirmation_resolved",
    confirmationId: "cf_1",
    decision: "approve",
  });
  assert.equal(session.pendingConfirmations.length, 1);
}

// resolved for unknown id is a no-op
{
  const { session, handlers } = makeDeps();
  handlers.handleSessionEvent({
    type: "confirmation_resolved",
    confirmationId: "cf_missing",
  });
  assert.equal(session.pendingConfirmations.length, 0);
}

console.log("confirmation event handler tests passed");
