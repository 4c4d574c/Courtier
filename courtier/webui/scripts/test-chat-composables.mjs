import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { rmSync, mkdirSync, readFileSync, writeFileSync, readdirSync, statSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { ref } from "vue";

const scriptDir = dirname(fileURLToPath(import.meta.url));
const rootDir = resolve(scriptDir, "..");
const outDir = resolve(rootDir, ".tmp/chat-composables-test");

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
      resolve(rootDir, "src/composables/useChatMessages.ts"),
      resolve(rootDir, "src/composables/useFilePreview.ts"),
      resolve(rootDir, "src/utils/chatMessages.ts"),
      resolve(rootDir, "src/utils/sessionUtils.ts"),
      resolve(rootDir, "src/constants/messages.ts"),
    ],
    { cwd: rootDir, stdio: "inherit" },
  );

  fixRelativeImports(outDir);

  const { useChatMessages } = await import(
    pathToFileURL(resolve(outDir, "composables/useChatMessages.js")).href
  );
  const { useFilePreview } = await import(
    pathToFileURL(resolve(outDir, "composables/useFilePreview.js")).href
  );

  // useFilePreview
  const preview = useFilePreview();
  assert.equal(preview.isOpen.value, false);
  assert.equal(preview.currentFile.value, null);

  const fileItem = {
    type: "file",
    id: "f-1",
    name: "doc.pdf",
    url: "blob://doc",
    mimeType: "application/pdf",
  };
  preview.open(fileItem);
  assert.equal(preview.isOpen.value, true);
  assert.deepEqual(preview.currentFile.value, fileItem);
  preview.close();
  assert.equal(preview.isOpen.value, false);
  assert.equal(preview.currentFile.value, null);

  // useChatMessages
  const baseSession = {
    id: "s1",
    task: "",
    modelName: "Claude",
    status: "completed",
    turns: [
      {
        message: { role: "user", text: "hello", timestamp: 1 },
        steps: [],
        conclusion: "done",
      },
    ],
    steps: [],
    thoughts: [],
    stats: { tokensIn: 0, tokensOut: 0, elapsed: 0 },
    createdAt: 1,
  };
  const fileRecords = ref([]);
  const { messages, title } = useChatMessages(baseSession, fileRecords);
  assert.equal(messages.value.length, 2);
  assert.equal(messages.value[0].type, "user");
  assert.equal(messages.value[1].type, "assistant");
  assert.equal(title.value, "hello");

  // Reactivity: adding a file record updates the file item when a turn references it
  const fileSession = {
    ...baseSession,
    turns: [
      {
        message: {
          role: "user",
          text: "with file",
          fileId: "f1",
          fileName: "note.pdf",
          timestamp: 1,
        },
        steps: [],
      },
    ],
  };
  const reactiveRecords = ref([]);
  const { messages: fileMessages } = useChatMessages(fileSession, reactiveRecords);
  assert.equal(fileMessages.value.length, 2);
  assert.equal(fileMessages.value[0].type, "user");
  assert.equal(fileMessages.value[1].type, "file");
  assert.equal(fileMessages.value[1].url, "");

  reactiveRecords.value = [
    { fileId: "f1", name: "note.pdf", url: "blob://note" },
  ];
  assert.equal(fileMessages.value.length, 2);
  assert.equal(fileMessages.value[1].type, "file");
  assert.equal(fileMessages.value[1].url, "blob://note");

  console.log("chatComposables verification passed");
} finally {
  rmSync(outDir, { recursive: true, force: true });
}
