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

// Capture EventSource constructions (client.ts builds the SSE URL there).
const esInstances = [];
class FakeEventSource {
  constructor(url, opts) {
    this.url = url;
    this.opts = opts;
    this.closed = false;
    this.readyState = 1; // OPEN
    esInstances.push(this);
  }
  close() {
    this.closed = true;
    this.readyState = 2; // CLOSED
  }
  emit(data) {
    this.onmessage?.({ data: JSON.stringify(data) });
  }
  fail() {
    this.readyState = 2; // server 404 at open → CLOSED
    this.onerror?.(new Event("error"));
  }
}
FakeEventSource.CLOSED = 2;
globalThis.EventSource = FakeEventSource;

// Stub authFetch so the fallback paths (loadSession) resolve without a server.
// Controlled per-test via loadSessionResponder.
let loadSessionResponder = async () => ({ ok: false, json: async () => null });
globalThis.fetch = async (input) => loadSessionResponder(input);

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

  // 1. Restoring a running session re-attaches with the snapshot's watermark.
  {
    const { session, restoreSession, isRunning } = useAgentSession();
    esInstances.length = 0;
    restoreSession(makeLoadedSession({ status: "running", eventSeq: 41 }));

    assert.equal(esInstances.length, 1);
    const url = esInstances[0].url;
    assert.ok(
      url.includes("/api/sessions/sess_run1/events"),
      `expected attach endpoint, got ${url}`,
    );
    assert.ok(url.includes("since=41"), `expected watermark in url, got ${url}`);
    assert.equal(isRunning.value, true);

    // Replay events flow through the normal pipeline.
    esInstances[0].emit({ type: "token", text: "流式文本" });
    await tick();
    assert.equal(session.thoughts.length, 1);
    esInstances[0].emit({ type: "complete", conclusion: "结论", tokensIn: 1, tokensOut: 1 });
    await tick();
    assert.equal(session.status, "completed");
  }

  // 2. Restoring a finished session does NOT attach.
  {
    const { restoreSession } = useAgentSession();
    esInstances.length = 0;
    restoreSession(makeLoadedSession({ status: "completed", eventSeq: 9 }));
    assert.equal(esInstances.length, 0);
    restoreSession(makeLoadedSession({ status: "interrupted", eventSeq: 9 }));
    assert.equal(esInstances.length, 0);
  }

  // 3. Restoring a queued session attaches as well; positions decrement as
  //    runs ahead finish; the first live event clears the queued state.
  {
    const { session, restoreSession, isRunning } = useAgentSession();
    esInstances.length = 0;
    restoreSession(makeLoadedSession({ status: "queued", eventSeq: 3 }));
    assert.equal(esInstances.length, 1);
    assert.ok(esInstances[0].url.includes("since=3"));
    // Queued sessions count as running for interaction gating (stop button).
    assert.equal(isRunning.value, true);
    esInstances[0].emit({ type: "queued", position: 2 });
    await tick();
    assert.equal(session.queuePosition, 2);
    esInstances[0].emit({ type: "queued", position: 1 });
    await tick();
    assert.equal(session.queuePosition, 1);
    esInstances[0].emit({ type: "queued", position: 0 });
    await tick();
    assert.equal(session.queuePosition, 0);
    esInstances[0].emit({ type: "token", text: "x" });
    await tick();
    assert.equal(session.queuePosition, undefined);
    esInstances[0].emit({ type: "complete", conclusion: "c" });
    await tick();
    assert.equal(session.queuePosition, undefined);
    assert.equal(session.status, "completed");
    assert.equal(isRunning.value, false);
  }

  // 4. Attach 404 (run gone) falls back to reloading the snapshot.
  {
    const { session, restoreSession } = useAgentSession();
    esInstances.length = 0;
    restoreSession(makeLoadedSession({ status: "running", eventSeq: 7 }));
    assert.equal(esInstances.length, 1);
    // Server answered 404: the stream never opened. The fallback reloads the
    // snapshot — which now says the run completed.
    loadSessionResponder = async () => ({
      ok: true,
      json: async () => makeLoadedSession({ status: "completed", eventSeq: 99, conclusion: "答案" }),
    });
    esInstances[0].fail();
    await tick();
    await tick();
    await tick();
    assert.equal(session.status, "completed");
    assert.equal(session.conclusion, "答案");
    // No further attach attempt (terminal snapshot).
    assert.equal(esInstances.length, 1);
  }

  // 5. resync event closes the stream, reloads once, re-attaches from the
  //    fresh watermark.
  {
    const { restoreSession } = useAgentSession();
    esInstances.length = 0;
    restoreSession(makeLoadedSession({ status: "running", eventSeq: 5 }));
    assert.equal(esInstances.length, 1);
    loadSessionResponder = async () => ({
      ok: true,
      json: async () => makeLoadedSession({ status: "running", eventSeq: 88 }),
    });
    esInstances[0].emit({ type: "resync" });
    await tick();
    await tick();
    await tick();
    assert.equal(esInstances[0].closed, true);
    assert.equal(esInstances.length, 2);
    assert.ok(esInstances[1].url.includes("since=88"), esInstances[1].url);
  }

  console.log("test-attach-resume: all assertions passed");
} finally {
  rmSync(outDir, { recursive: true, force: true });
}
