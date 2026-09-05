import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { rmSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const scriptDir = dirname(fileURLToPath(import.meta.url));
const rootDir = resolve(scriptDir, "..");
const outDir = resolve(rootDir, ".tmp/run-events-test");

rmSync(outDir, { recursive: true, force: true });
mkdirSync(outDir, { recursive: true });

const esInstances = [];
class FakeStream {
  constructor(url, init) {
    this.url = url;
    this.closed = false;
    this._pending = [];
    this._resolvers = [];
    esInstances.push(this);
    init?.signal?.addEventListener("abort", () => {
      this.closed = true;
      this._flush();
    });
  }
  close() {
    this.closed = true;
  }
  open() {
    this.onopen?.(new Event("open"));
  }
  emit(obj) {
    this._pending.push("id: 1\ndata: " + JSON.stringify(obj) + "\n\n");
    this._flush();
  }
  error() {
    this._failed = true;
    this._flush();
  }
  _flush() {
    for (const r of this._resolvers.splice(0)) r();
  }
}
globalThis.fetch = async (input, init) => {
  const url = typeof input === "string" ? input : input?.url ?? "";
  const stream = new FakeStream(url, init);
  return {
    ok: true,
    body: {
      getReader: () => ({
        read: async () => {
          for (;;) {
            if (stream._pending.length) {
              return { done: false, value: new TextEncoder().encode(stream._pending.shift()) };
            }
            if (stream._failed) {
              console.log("DEBUG read throwing, abort:", init?.signal?.aborted);
              throw new Error("network error");
            }
            if (stream._done) return { done: true };
            await new Promise((r) => stream._resolvers.push(r));
          }
        },
      }),
    },
  };
};

try {
  execFileSync(
    resolve(rootDir, "node_modules/.bin/esbuild"),
    [
      resolve(rootDir, "src/composables/useRunEvents.ts"),
      "--bundle",
      "--format=esm",
      "--platform=node",
      "--packages=external",
      "--outfile=" + resolve(outDir, "useRunEvents.bundle.js"),
    ],
    { cwd: rootDir, stdio: "inherit" },
  );

  const mod = await import(
    pathToFileURL(resolve(outDir, "useRunEvents.bundle.js")).href
  );
  const { useRunEvents } = mod;
  const tick = () => new Promise((r) => setTimeout(r, 0));

  // 1. start() opens one channel; statuses update per session; listeners fire.
  {
    const runEvents = useRunEvents();
    const events = [];
    const off = runEvents.onRunStatus((e) => events.push(e));
    runEvents.start();
    assert.equal(esInstances.length, 1);
    assert.ok(esInstances[0].url.endsWith("/api/events"), esInstances[0].url);

    esInstances[0].emit({
      type: "run_status",
      sessionId: "sess_a",
      status: "running",
    });
    await tick();
    assert.equal(runEvents.statuses["sess_a"].status, "running");

    esInstances[0].emit({
      type: "run_status",
      sessionId: "sess_a",
      status: "completed",
      conclusion: "结论",
      tokensIn: 10,
      tokensOut: 5,
    });
    await tick();
    assert.equal(runEvents.statuses["sess_a"].status, "completed");
    assert.equal(events.length, 2);
    assert.equal(events[1].sessionId, "sess_a");
    assert.equal(events[1].conclusion, "结论");
    assert.equal(events[1].tokensIn, 10);
    // seq increments so repeated transitions are observable.
    assert.ok(events[1].seq > events[0].seq);
    off();
    runEvents.stop();
    assert.equal(esInstances[0].closed, true);
    assert.equal(Object.keys(runEvents.statuses).length, 0);
  }

  // 2. Channel error → backoff timer re-opens; reconnect listeners fire
  //    only after the reopened channel delivers a message (successful
  //    realign), not on every failed error event.
  {
    const runEvents = useRunEvents();
    esInstances.length = 0;
    let refetched = 0;
    runEvents.onReconnect(() => refetched++);
    runEvents.start();
    assert.equal(esInstances.length, 1);
    esInstances[0].error();
    await new Promise((r) => setTimeout(r, 100));
    assert.equal(esInstances[0].closed, true);
    // Errors alone must not spam the list route.
    assert.equal(refetched, 0);
    // Timer-based re-open: wait past the 3s backoff.
    await new Promise((r) => setTimeout(r, 3200));
    assert.equal(esInstances.length, 2);
    // Reconnect success (onopen) realigns once...
    esInstances[1].open();
    await tick();
    assert.equal(refetched, 1);
    // ...and subsequent messages do not re-fire it.
    esInstances[1].emit({
      type: "run_status",
      sessionId: "s1",
      status: "completed",
    });
    await tick();
    assert.equal(refetched, 1);
    runEvents.stop();
  }

  // 3. Unknown payload shapes are ignored without breaking the channel.
  {
    const runEvents = useRunEvents();
    esInstances.length = 0;
    const events = [];
    runEvents.onRunStatus((e) => events.push(e));
    runEvents.start();
    esInstances[0].emit({ type: "something_else", x: 1 });
    esInstances[0].emit("not json at all");
    await tick();
    assert.equal(events.length, 0);
    assert.equal(esInstances[0].closed, false);
    runEvents.stop();
  }

  console.log("test-run-events: all assertions passed");
} finally {
  rmSync(outDir, { recursive: true, force: true });
}
