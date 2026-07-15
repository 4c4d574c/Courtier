import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { rmSync, mkdirSync, readFileSync, writeFileSync, readdirSync, statSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const scriptDir = dirname(fileURLToPath(import.meta.url));
const rootDir = resolve(scriptDir, "..");
const outDir = resolve(rootDir, ".tmp/chat-messages-test");

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

try {
  execFileSync(
    process.execPath,
    [
      resolve(rootDir, "node_modules/typescript/bin/tsc"),
      "--target",
      "ES2020",
      "--module",
      "ES2020",
      "--moduleResolution",
      "bundler",
      "--strict",
      "--skipLibCheck",
      "--outDir",
      outDir,
      "--rootDir",
      resolve(rootDir, "src"),
      resolve(rootDir, "src/utils/chatMessages.ts"),
      resolve(rootDir, "src/utils/sessionUtils.ts"),
      resolve(rootDir, "src/constants/messages.ts"),
    ],
    { cwd: rootDir, stdio: "inherit" },
  );

  fixRelativeImports(outDir);

  const chatMessages = await import(
    pathToFileURL(resolve(outDir, "utils/chatMessages.js")).href
  );
  const { buildChatMessages, deriveConversationTitle, fileMimeType } =
    chatMessages;

  const baseSession = {
    id: "s1",
    task: "审核合同",
    modelName: "Claude",
    status: "completed",
    turns: [],
    steps: [],
    thoughts: [],
    stats: { tokensIn: 0, tokensOut: 0, elapsed: 0 },
    createdAt: 1,
  };

  // Title from task
  assert.equal(deriveConversationTitle(baseSession), "审核合同");

  // Title fallback to first user message
  const titleSession = {
    ...baseSession,
    task: "",
    turns: [
      {
        message: { role: "user", text: "检查格式", timestamp: 1 },
        steps: [],
      },
    ],
  };
  assert.equal(deriveConversationTitle(titleSession), "检查格式");

  // User + file + assistant
  const fileRecords = [
    { fileId: "f1", name: "notice.pdf", url: "blob://notice" },
  ];
  const fileSession = {
    ...baseSession,
    turns: [
      {
        message: {
          role: "user",
          text: "审核这份通知",
          fileId: "f1",
          fileName: "notice.pdf",
          timestamp: 1,
        },
        steps: [],
        conclusion: "格式正确",
      },
    ],
  };
  const msgs = buildChatMessages(fileSession, fileRecords);
  assert.equal(msgs.length, 3);
  assert.equal(msgs[0].type, "user");
  assert.equal(msgs[1].type, "file");
  assert.equal(msgs[1].url, "blob://notice");
  assert.equal(msgs[1].mimeType, "application/pdf");
  assert.equal(msgs[2].type, "assistant");
  assert.equal(msgs[2].content, "格式正确");

  // MIME helper
  assert.equal(fileMimeType("photo.jpg"), "image/jpeg");
  assert.equal(fileMimeType("doc.docx"), "application/octet-stream");

  // Thinking item from session.thoughts matching turn and stepIndex
  const thoughtSession = {
    ...baseSession,
    turns: [
      {
        message: { role: "user", text: "思考测试", timestamp: 1 },
        steps: [
          {
            index: 10,
            numeral: "1",
            label: "step",
            skill: "s",
            tools: [],
            turnIndex: 0,
          },
        ],
      },
    ],
    thoughts: [
      {
        id: 1,
        text: "正在分析...",
        turn: 1,
        turnIndex: 0,
        stepIndex: 10,
        timestamp: 1,
      },
    ],
  };
  const thoughtMsgs = buildChatMessages(thoughtSession, []);
  assert.equal(thoughtMsgs.length, 2);
  assert.equal(thoughtMsgs[0].type, "user");
  assert.equal(thoughtMsgs[1].type, "thinking");
  assert.equal(thoughtMsgs[1].content, "正在分析...");
  assert.equal(thoughtMsgs[1].isOpen, true);

  // Error item from completed session with errorMessage
  const errorSession = {
    ...baseSession,
    status: "error",
    errorMessage: "引擎异常",
    turns: [
      {
        message: { role: "user", text: "报错测试", timestamp: 1 },
        steps: [],
      },
    ],
  };
  const errorMsgs = buildChatMessages(errorSession, []);
  assert.equal(errorMsgs.length, 2);
  assert.equal(errorMsgs[0].type, "user");
  assert.equal(errorMsgs[1].type, "error");
  assert.equal(errorMsgs[1].title, "会话异常终止");
  assert.equal(errorMsgs[1].detail, "引擎异常");

  // Stopped item from completed session with stopReason: "user"
  const stoppedSession = {
    ...baseSession,
    status: "completed",
    stopReason: "user",
    turns: [
      {
        message: { role: "user", text: "停止测试", timestamp: 1 },
        steps: [],
      },
    ],
  };
  const stoppedMsgs = buildChatMessages(stoppedSession, []);
  assert.equal(stoppedMsgs.length, 2);
  assert.equal(stoppedMsgs[0].type, "user");
  assert.equal(stoppedMsgs[1].type, "stopped");
  assert.equal(stoppedMsgs[1].title, "会话已中断");
  assert.equal(stoppedMsgs[1].detail, "由用户手动停止");

  // Running session's last turn with no conclusion produces assistant placeholder
  const runningSession = {
    ...baseSession,
    status: "running",
    turns: [
      {
        message: { role: "user", text: "运行测试", timestamp: 1 },
        steps: [],
      },
    ],
  };
  const runningMsgs = buildChatMessages(runningSession, []);
  assert.equal(runningMsgs.length, 2);
  assert.equal(runningMsgs[0].type, "user");
  assert.equal(runningMsgs[1].type, "assistant");
  assert.equal(runningMsgs[1].content, "");

  console.log("chatMessages verification passed");
} finally {
  rmSync(outDir, { recursive: true, force: true });
}
