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

  console.log("chatMessages verification passed");
} finally {
  rmSync(outDir, { recursive: true, force: true });
}
