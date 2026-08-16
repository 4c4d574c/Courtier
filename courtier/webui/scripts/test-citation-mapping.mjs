import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { rmSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const scriptDir = dirname(fileURLToPath(import.meta.url));
const rootDir = resolve(scriptDir, "..");
const outDir = resolve(rootDir, ".tmp/citation-mapping-test");

rmSync(outDir, { recursive: true, force: true });
mkdirSync(outDir, { recursive: true });

try {
  for (const [entry, outfile] of [
    ["src/composables/sessionEventHandlers/index.ts", "handlers.js"],
    ["src/utils/chatMessages.ts", "chatMessages.js"],
  ]) {
    execFileSync(
      resolve(rootDir, "node_modules/.bin/esbuild"),
      [
        resolve(rootDir, entry),
        "--bundle",
        "--format=esm",
        "--platform=node",
        "--outfile=" + resolve(outDir, outfile),
      ],
      { cwd: rootDir, stdio: "inherit" },
    );
  }

  const handlersMod = await import(
    pathToFileURL(resolve(outDir, "handlers.js")).href
  );
  const chatMod = await import(
    pathToFileURL(resolve(outDir, "chatMessages.js")).href
  );
  const { createSessionEventHandlers } = handlersMod;
  const { buildChatMessages } = chatMod;

  function makeDeps() {
    const session = {
      id: "",
      task: "检索",
      modelName: "",
      status: "running",
      turns: [],
      steps: [],
      thoughts: [],
      stats: { tokensIn: 0, tokensOut: 0, elapsed: 0 },
      createdAt: Date.now(),
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
    const turnVersion = { value: 0 };
    const handlers = createSessionEventHandlers(() => ({
      state,
      session,
      turnVersion,
    }));
    return { handlers, session };
  }

  // Replay the sess_8146c3be5274 shape: 4 same-name search calls, results
  // arrive in execution order 1..4 (call 3 empty), model numbers hits
  // cumulatively (1..10, 11..19, -, 20..23).
  const calls = [
    { id: "c1", offset: 0, n: 10 },
    { id: "c2", offset: 10, n: 9 },
    { id: "c3", offset: null, n: 0 },
    { id: "c4", offset: 19, n: 4 },
  ];
  const hitTitle = (call, i) => `doc_${call.id}_${i}`;

  const { handlers, session } = makeDeps();
  handlers.handleSessionEvent({
    type: "think",
    toolCalls: ["search_documents", "search_documents", "search_documents", "search_documents"],
    toolCallIds: calls.map((c) => c.id),
  });
  handlers.handleSessionEvent({ type: "act", tools: ["search_documents"] });

  for (const call of calls) {
    const event = {
      type: "tool_result",
      name: "search_documents",
      status: "ok",
      callKind: "tool",
      callScope: "parent",
      summary: `call ${call.id}`,
      toolCallId: call.id,
    };
    if (call.n > 0) {
      event.citations = Array.from({ length: call.n }, (_, i) => ({
        resourceId: i,
        documentId: null,
        title: hitTitle(call, i),
        chunkText: `text ${call.id} ${i}`,
      }));
      event.citationOffset = call.offset;
    }
    handlers.handleSessionEvent(event);
  }

  const cards = session.steps[0].tools;
  assert.equal(cards.length, 4);
  // Pairing: card i carries call i's result (name-based matching would
  // attach them in reverse).
  cards.forEach((card, i) => {
    assert.equal(card.toolCallId, calls[i].id);
    assert.equal(card.summary, `call ${calls[i].id}`);
    assert.equal(card.citations?.length ?? 0, calls[i].n);
    if (calls[i].n > 0) {
      assert.equal(card.citations[0].title, hitTitle(calls[i], 0));
      assert.equal(card.citationOffset, calls[i].offset);
    }
  });

  // Resolution: absolute numbers map to the right hits regardless of order.
  session.turns.push({
    message: { text: "检索", fileName: undefined },
    steps: session.steps,
    conclusion: "结论 [[1]] [[11]] [[13]] [[21]]",
  });
  session.status = "completed";
  const items = buildChatMessages(session, []);
  const assistant = items.find(
    (it) => it.type === "assistant" && it.content.includes("[[21]]"),
  );
  assert.ok(assistant?.citations, "assistant item carries citations");
  const { byNumber, list } = assistant.citations;
  assert.equal(list.length, 23);
  // [[1]] is call 1's first hit (not call 4's, which merge order would
  // give without ids).
  assert.equal(byNumber.get(1)?.title, hitTitle(calls[0], 0));
  // [[11]]/[[13]] land in call 2's range (offset 10 → indices 11..19).
  assert.equal(byNumber.get(11)?.title, hitTitle(calls[1], 0));
  assert.equal(byNumber.get(13)?.title, hitTitle(calls[1], 2));
  // [[21]] in call 4's range (offset 19 → 20..23).
  assert.equal(byNumber.get(21)?.title, hitTitle(calls[3], 1));
  assert.equal(byNumber.get(23)?.title, hitTitle(calls[3], 3));
  assert.equal(byNumber.get(24), undefined);

  // Legacy fallback: events without ids/offsets still resolve via merge
  // order (sequential numbering).
  {
    const { handlers, session } = makeDeps();
    handlers.handleSessionEvent({
      type: "think",
      toolCalls: ["search_documents", "search_documents"],
    });
    handlers.handleSessionEvent({ type: "act", tools: ["search_documents"] });
    for (const n of [3, 2]) {
      handlers.handleSessionEvent({
        type: "tool_result",
        name: "search_documents",
        status: "ok",
        callKind: "tool",
        callScope: "parent",
        summary: "legacy",
        citations: Array.from({ length: n }, (_, i) => ({ title: `legacy_${i}` })),
      });
    }
    session.turns.push({
      message: { text: "q", fileName: undefined },
      steps: session.steps,
      conclusion: "旧事件 [[4]]",
    });
    session.status = "completed";
    const items = buildChatMessages(session, []);
    const assistant = items.find((it) => it.type === "assistant");
    assert.equal(assistant?.citations?.byNumber.get(4)?.title, "legacy_1");
  }

  console.log("test-citation-mapping: all assertions passed");
} finally {
  rmSync(outDir, { recursive: true, force: true });
}
