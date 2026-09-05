import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { rmSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const scriptDir = dirname(fileURLToPath(import.meta.url));
const rootDir = resolve(scriptDir, "..");
const outDir = resolve(rootDir, ".tmp/attach-resume-test");

rmSync(outDir, { recursive: true, force: true });
mkdirSync(outDir, { recursive: true });

// --- fetch stub -----------------------------------------------------------
// The run/attach SSE transport is now fetch-based (SseFetchClient): tests
// drive controllable fake streams through it.

// loadSessionResponder controls the snapshot-reload fallback path.
let loadSessionResponder = async () => ({ ok: false, json: async () => null });
// "notfound" makes the next /events fetch answer 404 at open (fail-closed).
let eventsMode = "open";

const attachFetches = [];
function makeFakeStream(url, init) {
  const stream = {
    url,
    closed: false,
    _pending: [],
    _resolvers: [],
    _done: false,
    _failed: false,
    push(obj) {
      stream._pending.push("id: 1\ndata: " + JSON.stringify(obj) + "\n\n");
      stream._flush();
    },
    end() {
      stream._done = true;
      stream._flush();
    },
  };
  async function read() {
    for (;;) {
      if (stream._pending.length) {
        return { done: false, value: new TextEncoder().encode(stream._pending.shift()) };
      }
      if (stream._failed) throw new Error("network error");
      if (stream._done) return { done: true };
      await new Promise((r) => stream._resolvers.push(r));
    }
  }
  stream._flush = () => {
    for (const r of stream._resolvers.splice(0)) r();
  };
  stream.res = {
    ok: true,
    body: {
      getReader: () => {
        const signal = init?.signal;
        if (signal) {
          signal.addEventListener("abort", () => {
            stream.aborted = true;
            stream._flush();
          });
        }
        return { read };
      },
    },
  };
  return stream;
}

globalThis.fetch = async (input, init) => {
  const url = typeof input === "string" ? input : input?.url ?? "";
  if (url.includes("/events")) {
    attachFetches.push({ url, stream: null });
    if (eventsMode === "notfound") {
      // 404 at open → the client fail-closes (CLOSED semantics).
      return { ok: false, status: 404, json: async () => ({}), body: null };
    }
    const stream = makeFakeStream(url, init);
    attachFetches[attachFetches.length - 1].stream = stream;
    return stream.res;
  }
  if (url.includes("/api/sessions/sess_run1/events") || url.includes("/api/sessions/")) {
    // unreachable — handled above; kept for clarity
  }
  return loadSessionResponder(input);
};

async function drainAttach(apply) {
  // Wait until the attach fetch has a stream, apply the frames, and let
  // the composable settle.
  await tick();
  apply(attachFetches.at(-1));
}

const tick = () => new Promise((r) => setTimeout(r, 0));
  const settle = async (n = 8) => { for (let i = 0; i < n; i++) await tick(); };

try {
  execFileSync(
    resolve(rootDir, "node_modules/.bin/esbuild"),
    [
      resolve(rootDir, "src/composables/useAgentSession.ts"),
      "--bundle",
      "--format=esm",
      "--platform=node",
      "--packages=external",
      "--define:import.meta.env={\"DEV\":false}",
      "--outfile=" + resolve(outDir, "useAgentSession.bundle.js"),
    ],
    { cwd: rootDir, stdio: "inherit" },
  );

  const mod = await import(
    pathToFileURL(resolve(outDir, "useAgentSession.bundle.js")).href
  );
  const { useAgentSession } = mod;

  function makeLoadedSession(overrides = {}) {
    return {
      id: "sess_run1",
      task: "审计文档",
      modelName: "m",
      status: "completed",
      turns: [
        {
          message: { role: "user", text: "审计文档", timestamp: 1 },
          steps: [],
        },
      ],
      steps: [],
      thoughts: [],
      stats: { tokensIn: 0, tokensOut: 0, elapsed: 0 },
      createdAt: 1,
      contextCompacted: false,
      ...overrides,
    };
  }

  const tick = () => new Promise((r) => setTimeout(r, 0));
  const settle = async (n = 8) => { for (let i = 0; i < n; i++) await tick(); };

  // 1. Restoring a running session re-attaches with the snapshot's watermark.
  {
    const { session, restoreSession, isRunning } = useAgentSession();
    attachFetches.length = 0;
    try {
      restoreSession(makeLoadedSession({ status: "running", eventSeq: 41 }));
    } catch (e) {
    }
    await settle(8);

    assert.equal(attachFetches.length, 1);
    const url = attachFetches[0].url;
    assert.ok(
      url.includes("/api/sessions/sess_run1/events"),
      `expected attach endpoint, got ${url}`,
    );
    assert.ok(url.includes("since=41"), `expected watermark in url, got ${url}`);
    assert.equal(isRunning.value, true);

    // Replay events flow through the normal pipeline.
    attachFetches[0].stream.push({ type: "token", text: "流式文本" });
    await settle(8);
    await settle(8);
    assert.equal(session.thoughts.length, 1);
    attachFetches[0].stream.push({ type: "complete", conclusion: "结论", tokensIn: 1, tokensOut: 1 });
    await settle(6);
    assert.equal(session.status, "completed");
  }

  // 2. Restoring a finished session does NOT attach.
  {
    const { restoreSession } = useAgentSession();
    attachFetches.length = 0;
    restoreSession(makeLoadedSession({ status: "completed", eventSeq: 9 }));
    assert.equal(attachFetches.length, 0);
    restoreSession(makeLoadedSession({ status: "interrupted", eventSeq: 9 }));
    assert.equal(attachFetches.length, 0);
  }

  // 3. Restoring a queued session attaches as well; positions decrement as
  //    runs ahead finish; the first live event clears the queued state.
  {
    const { session, restoreSession, isRunning } = useAgentSession();
    attachFetches.length = 0;
    restoreSession(makeLoadedSession({ status: "queued", eventSeq: 3 }));
    await settle(8);
    assert.equal(attachFetches.length, 1);
    assert.ok(attachFetches[0].url.includes("since=3"));
    // Queued sessions count as running for interaction gating (stop button).
    assert.equal(isRunning.value, true);
    attachFetches[0].stream.push({ type: "queued", position: 2 });
    await settle(6);
    assert.equal(session.queuePosition, 2);
    attachFetches[0].stream.push({ type: "queued", position: 1 });
    await settle(6);
    assert.equal(session.queuePosition, 1);
    attachFetches[0].stream.push({ type: "queued", position: 0 });
    await settle(6);
    assert.equal(session.queuePosition, 0);
    attachFetches[0].stream.push({ type: "token", text: "x" });
    await settle(6);
    assert.equal(session.queuePosition, undefined);
    attachFetches[0].stream.push({ type: "complete", conclusion: "c" });
    await settle(6);
    assert.equal(session.queuePosition, undefined);
    assert.equal(session.status, "completed");
    assert.equal(isRunning.value, false);
  }

  // 4. Attach 404 (run gone) falls back to reloading the snapshot.
  {
    const { session, restoreSession } = useAgentSession();
    attachFetches.length = 0;
    eventsMode = "notfound";
    // The fallback snapshot: reload sees the run already completed.
    loadSessionResponder = async () => ({
      ok: true,
      json: async () => makeLoadedSession({ status: "completed", eventSeq: 99, conclusion: "答案" }),
    });
    restoreSession(makeLoadedSession({ status: "running", eventSeq: 7 }));
    await settle(8);
    assert.equal(attachFetches.length, 1);
    await settle(8);
    eventsMode = "open";
    assert.equal(session.status, "completed");
    assert.equal(session.conclusion, "答案");
    // No further attach attempt (terminal snapshot).
    assert.equal(attachFetches.length, 1);
  }

  // 5. resync event closes the stream, reloads once, re-attaches from the
  //    fresh watermark.
  {
    const { restoreSession } = useAgentSession();
    attachFetches.length = 0;
    eventsMode = "open";
    restoreSession(makeLoadedSession({ status: "running", eventSeq: 5 }));
    await settle(8);
    assert.equal(attachFetches.length, 1);
    loadSessionResponder = async () => ({
      ok: true,
      json: async () => makeLoadedSession({ status: "running", eventSeq: 88 }),
    });
    attachFetches[0].stream.push({ type: "resync" });
    await settle(8);
    await tick();
    // The composable closed the stream via abort on receiving resync.
    assert.equal(attachFetches[0].stream.aborted, true);
    assert.equal(attachFetches.length, 2);
    assert.ok(attachFetches[1].url.includes("since=88"), attachFetches[1].url);
  }

  // 6. Replayed events attach to the snapshot's in-flight turn (the render
  //    path): steps land in turns[last].steps with collision-free ids,
  //    streamed tokens continue the last restored thought, and the final
  //    conclusion lands on the turn — not just on session-level state.
  {
    const { session, restoreSession } = useAgentSession();
    attachFetches.length = 0;
    const snapshotStep = {
      index: 1,
      numeral: "一",
      label: "parse_layout",
      skill: "",
      turnIndex: 1,
      startSegmentIndex: 3,
      tools: [
        {
          id: "tool-3",
          name: "parse_layout",
          status: "done",
          callKind: "tool",
          callScope: "orchestrator",
        },
      ],
    };
    restoreSession(
      makeLoadedSession({
        status: "running",
        eventSeq: 12,
        turns: [
          {
            message: { role: "user", text: "审计文档", timestamp: 1 },
            steps: [snapshotStep],
          },
        ],
        steps: [snapshotStep],
        thoughts: [
          {
            id: 5,
            text: "前面的思考",
            turn: 3,
            turnIndex: 1,
            segmentIndex: 4,
            segmentType: "observe",
            timestamp: 1,
          },
        ],
      }),
    );
    await settle(8);
    assert.equal(attachFetches.length, 1);

    // The next think creates a step INSIDE the snapshot turn, with an id
    // continuing the snapshot's tool counter (no tool-3 collision).
    attachFetches[0].stream.push({
      type: "think",
      toolCalls: ["search_documents"],
      toolCallIds: ["call-1"],
    });
    await settle(6);
    assert.equal(session.turns[0].steps.length, 2);
    const replayedTool = session.turns[0].steps[1].tools[0];
    assert.equal(replayedTool.name, "search_documents");
    assert.equal(replayedTool.id, "tool-4");

    // tool_start flips the card inside the turn's step.
    attachFetches[0].stream.push({
      type: "tool_start",
      name: "search_documents",
      toolCallId: "call-1",
    });
    await settle(6);
    assert.equal(session.turns[0].steps[1].tools[0].status, "running");

    // Streamed tokens continue the last restored thought (no new block).
    attachFetches[0].stream.push({ type: "token", text: "，续流文本" });
    await settle(6);
    assert.equal(session.thoughts.length, 1);
    assert.equal(session.thoughts[0].text, "前面的思考，续流文本");

    attachFetches[0].stream.push({ type: "conclusion_token", text: "中间结论" });
    await settle(6);
    attachFetches[0].stream.push({ type: "complete", conclusion: "最终结论" });
    await settle(6);
    assert.equal(session.status, "completed");
    assert.equal(session.turns[0].conclusion, "中间结论");
  }

  // 7. Replayed sub-agent events update the running node the snapshot
  //    restored inside the in-flight step (backend persists the tree per
  //    event — invariant I2 — so the node exists at attach time).
  {
    const { session, restoreSession } = useAgentSession();
    attachFetches.length = 0;
    const runningSubagent = {
      name: "format_auditor",
      displayName: "格式审核",
      handleId: "sa-1",
      task: "审核格式",
      status: "running",
      conclusion: "",
      error: "",
      thoughts: [{ id: 1, text: "已还原的思考" }],
      children: [],
      tools: [],
    };
    const subagentStep = {
      index: 1,
      numeral: "一",
      label: "run_format_auditor",
      skill: "format_audit",
      turnIndex: 1,
      startSegmentIndex: 1,
      tools: [
        {
          id: "tool-2",
          name: "run_format_auditor",
          status: "running",
          callKind: "tool",
          callScope: "parent",
        },
      ],
      subagents: [runningSubagent],
    };
    restoreSession(
      makeLoadedSession({
        status: "running",
        eventSeq: 20,
        turns: [
          {
            message: { role: "user", text: "审计文档", timestamp: 1 },
            steps: [subagentStep],
          },
        ],
        steps: [subagentStep],
        thoughts: [],
      }),
    );
    await settle(8);
    assert.equal(attachFetches.length, 1);

    // Streamed sub-agent tokens continue the restored node's thought block.
    attachFetches[0].stream.push({
      type: "subagent_token",
      name: "format_auditor",
      handleId: "sa-1",
      text: "，重连后续流",
    });
    await settle(6);
    const node = () => session.turns[0].steps[0].subagents[0];
    assert.equal(node().thoughts.at(-1).text, "已还原的思考，重连后续流");

    // Sub-agent tool results land in the node inside the turn's step.
    attachFetches[0].stream.push({
      type: "subagent_tool_result",
      name: "format_auditor",
      handleId: "sa-1",
      toolName: "check_format",
      toolStatus: "ok",
      toolSummary: "3 处问题",
    });
    await settle(6);
    assert.equal(node().tools.length, 1);
    assert.equal(node().tools[0].name, "check_format");

    // Sub-agent end flips the node's status in place.
    attachFetches[0].stream.push({
      type: "subagent_end",
      name: "format_auditor",
      handleId: "sa-1",
      result: { status: "completed" },
    });
    await tick();
    assert.equal(node().status, "completed");
  }

  console.log("test-attach-resume: all assertions passed");
} finally {
  rmSync(outDir, { recursive: true, force: true });
}
