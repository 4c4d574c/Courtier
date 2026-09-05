import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { rmSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const scriptDir = dirname(fileURLToPath(import.meta.url));
const rootDir = resolve(scriptDir, "..");
const outDir = resolve(rootDir, ".tmp/edit-resend-test");

rmSync(outDir, { recursive: true, force: true });
mkdirSync(outDir, { recursive: true });

// Capture fetch calls: run-start now goes through POST (SseFetchClient).
const fetchCalls = [];
globalThis.fetch = async (url, init) => {
  fetchCalls.push({
    url: String(url),
    body: init?.body ? JSON.parse(init.body) : null,
  });
  // A stream that ends immediately: the client settles as CLOSED.
  const reader = { read: async () => ({ done: true, value: undefined }) };
  return {
    ok: true,
    body: { getReader: () => reader },
    headers: new Map(),
  };
};

try {
  execFileSync(
    resolve(rootDir, "node_modules/.bin/esbuild"),
    [
      resolve(rootDir, "src/composables/useAgentSession.ts"),
      "--bundle",
      "--format=esm",
      "--platform=node",
      "--packages=external",
      "--outfile=" + resolve(outDir, "useAgentSession.bundle.js"),
    ],
    { cwd: rootDir, stdio: "inherit" },
  );

  const mod = await import(
    pathToFileURL(resolve(outDir, "useAgentSession.bundle.js")).href
  );
  const { useAgentSession } = mod;

  function makeLoadedSession() {
    const step = (index, turnIndex) => ({
      index,
      label: `s${index}`,
      skill: "",
      tools: [],
      turnIndex,
    });
    return {
      id: "sess_abc",
      task: "第一问",
      modelName: "m",
      status: "completed",
      turns: [
        {
          message: {
            role: "user",
            text: "第一问",
            fileName: "报告.pdf",
            fileId: "f1",
            timestamp: 1,
          },
          steps: [step(1, 0)],
          conclusion: "答一",
        },
        {
          message: { role: "user", text: "第二问", timestamp: 2 },
          steps: [step(2, 1)],
          conclusion: "答二",
        },
        {
          message: { role: "user", text: "第三问", timestamp: 3 },
          steps: [step(3, 2)],
          conclusion: "答三",
        },
      ],
      steps: [step(1, 0), step(2, 1), step(3, 2)],
      // turnIndex is 1-based in production (connect increments the counter
      // before pushing the first turn's thought).
      thoughts: [
        { id: 1, text: "思一", turn: 1, turnIndex: 1, timestamp: 1 },
        { id: 2, text: "思二", turn: 2, turnIndex: 2, timestamp: 2 },
        { id: 3, text: "思三", turn: 3, turnIndex: 3, timestamp: 3 },
      ],
      stats: { tokensIn: 0, tokensOut: 0, elapsed: 0 },
      createdAt: 1,
      contextCompacted: false,
    };
  }

  // 1. Editing a middle turn: local truncation mirrors the server, the edited
  //    text starts a fresh turn, and the SSE request carries editTurn.
  {
    const { session, restoreSession, editAndResend } = useAgentSession();
    restoreSession(makeLoadedSession());
    fetchCalls.length = 0;

    editAndResend(1, "改后的第二问");

    assert.equal(session.turns.length, 2);
    assert.equal(session.turns[0].message.text, "第一问");
    assert.equal(session.turns[1].message.text, "改后的第二问");
    // Turn-derived state rebuilt from surviving turns only.
    assert.deepEqual(
      session.steps.map((s) => s.index),
      [1],
    );
    assert.deepEqual(
      session.thoughts.map((t) => t.text),
      ["思一"],
    );
    assert.equal(session.conclusion, "答一");
    assert.equal(session.status, "running");
    assert.equal(fetchCalls.length, 1);
    // Run-start is a POST: params ride in the JSON body, not the URL.
    assert.equal(fetchCalls[0].url, "/api/sessions/run");
    assert.equal(fetchCalls[0].body.editTurn, 1);
    assert.equal(fetchCalls[0].body.sessionId, "sess_abc");
    assert.equal(fetchCalls[0].body.task, "改后的第二问");
    assert.equal(fetchCalls[0].body.fileId, undefined);
  }

  // 2. Editing the first turn keeps its upload and retitles the session.
  {
    const { session, restoreSession, editAndResend } = useAgentSession();
    restoreSession(makeLoadedSession());
    fetchCalls.length = 0;

    editAndResend(0, "改后的第一问");

    assert.equal(session.turns.length, 1);
    assert.equal(session.turns[0].message.text, "改后的第一问");
    assert.equal(session.turns[0].message.fileName, "报告.pdf");
    assert.equal(session.turns[0].message.fileId, "f1");
    assert.equal(session.task, "改后的第一问");
    assert.equal(session.steps.length, 0);
    assert.equal(session.thoughts.length, 0);
    assert.equal(session.conclusion, undefined);
    assert.equal(fetchCalls[0].body.editTurn, 0);
    assert.equal(fetchCalls[0].body.fileId, "f1");
  }

  // 3. Compacted session: edit is refused with the hint, nothing is truncated.
  {
    const { session, restoreSession, editAndResend, isCompacted } =
      useAgentSession();
    restoreSession(makeLoadedSession());
    session.compactions = [{ text: "x", turnIndex: 1, timestamp: 0 }];
    assert.equal(isCompacted.value, true);
    fetchCalls.length = 0;

    editAndResend(1, "改后");

    assert.equal(session.turns.length, 3);
    assert.ok(session.errorMessage.includes("已压缩"));
    assert.equal(fetchCalls.length, 0);
  }

  // 3b. The persisted flag gates the same way (restored compacted session).
  {
    const { session, restoreSession, editAndResend, isCompacted } =
      useAgentSession();
    const loaded = makeLoadedSession();
    loaded.contextCompacted = true;
    restoreSession(loaded);
    assert.equal(isCompacted.value, true);
    fetchCalls.length = 0;

    editAndResend(1, "改后");

    assert.equal(session.turns.length, 3);
    assert.equal(fetchCalls.length, 0);
  }

  // 4. Running session: edit is a no-op.
  {
    const { session, restoreSession, editAndResend } = useAgentSession();
    restoreSession(makeLoadedSession());
    session.status = "running";
    fetchCalls.length = 0;

    editAndResend(1, "改后");

    assert.equal(session.turns.length, 3);
    assert.equal(fetchCalls.length, 0);
  }

  // 5. Out-of-range turn index: no-op.
  {
    const { session, restoreSession, editAndResend } = useAgentSession();
    restoreSession(makeLoadedSession());
    fetchCalls.length = 0;

    editAndResend(5, "改后");

    assert.equal(session.turns.length, 3);
    assert.equal(fetchCalls.length, 0);
  }

  console.log("editResend verification passed");
} finally {
  rmSync(outDir, { recursive: true, force: true });
}
