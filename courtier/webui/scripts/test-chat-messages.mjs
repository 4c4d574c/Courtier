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
  // The uploaded file renders above the user prompt.
  assert.equal(msgs[0].type, "file");
  assert.equal(msgs[0].url, "blob://notice");
  assert.equal(msgs[0].mimeType, "application/pdf");
  assert.equal(msgs[1].type, "user");
  assert.equal(msgs[2].type, "assistant");
  assert.equal(msgs[2].content, "格式正确");

  // MIME helper
  assert.equal(fileMimeType("photo.jpg"), "image/jpeg");
  assert.equal(
    fileMimeType("doc.docx"),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  );

  // Thinking is attached to its step inside the steps process block
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
  assert.equal(thoughtMsgs[1].type, "steps");
  assert.equal(thoughtMsgs[1].groups.length, 1);
  assert.equal(thoughtMsgs[1].groups[0].step.index, 10);
  assert.equal(thoughtMsgs[1].groups[0].thoughts.length, 1);
  assert.equal(thoughtMsgs[1].groups[0].thoughts[0].text, "正在分析...");

  // Step verdicts render BEFORE their step's frame (text previews the tools):
  // un-announced steps join the open frame below the last text.
  const verdictTool = {
    id: "t1",
    name: "parse",
    skill: "",
    status: "done",
    callKind: "tool",
    callScope: "parent",
    subagentName: null,
  };
  const verdictSession = {
    ...baseSession,
    turns: [
      {
        message: { role: "user", text: "中间文本", timestamp: 1 },
        steps: [
          {
            index: 1,
            numeral: "1",
            label: "a",
            skill: "",
            tools: [verdictTool],
            turnIndex: 0,
            verdict: "中间结论一",
          },
          {
            index: 2,
            numeral: "2",
            label: "b",
            skill: "",
            tools: [{ ...verdictTool, id: "t2" }],
            turnIndex: 0,
          },
          {
            index: 3,
            numeral: "3",
            label: "c",
            skill: "",
            tools: [{ ...verdictTool, id: "t3" }],
            turnIndex: 0,
            verdict: "中间结论三",
          },
        ],
      },
    ],
  };
  const verdictMsgs = buildChatMessages(verdictSession, []);
  assert.deepEqual(
    verdictMsgs.map((m) => m.type),
    ["user", "assistant", "steps", "assistant", "steps"],
  );
  assert.equal(verdictMsgs[1].content, "中间结论一");
  assert.equal(verdictMsgs[2].groups.length, 2); // step 2 无预告文本，并入当前框
  assert.equal(verdictMsgs[2].groups[0].step.index, 1);
  assert.equal(verdictMsgs[2].groups[1].step.index, 2);
  assert.equal(verdictMsgs[3].content, "中间结论三");
  assert.equal(verdictMsgs[4].groups.length, 1);
  assert.equal(verdictMsgs[4].groups[0].step.index, 3);

  // verdict step 拆分：思考（先于文本产生）留在上方框，工具开文本下方新框
  const splitSession = {
    ...baseSession,
    turns: [
      {
        message: { role: "user", text: "拆分", timestamp: 1 },
        steps: [
          {
            index: 1,
            numeral: "1",
            label: "parse",
            skill: "",
            tools: [{ ...verdictTool, id: "s1" }],
            turnIndex: 0,
          },
          {
            index: 2,
            numeral: "2",
            label: "audit",
            skill: "",
            tools: [{ ...verdictTool, id: "s2" }],
            subagents: [
              { name: "完整审核", handleId: "h1", task: "审核", status: "running" },
            ],
            turnIndex: 0,
            verdict: "现在并行执行审核：",
          },
        ],
      },
    ],
    thoughts: [
      { id: 1, text: "先解析文档", turn: 1, turnIndex: 0, stepIndex: 1, timestamp: 1 },
      {
        id: 2,
        text: "决定并行调用子代理",
        turn: 2,
        turnIndex: 0,
        stepIndex: 2,
        timestamp: 2,
      },
    ],
  };
  const splitMsgs = buildChatMessages(splitSession, []);
  assert.deepEqual(
    splitMsgs.map((m) => m.type),
    ["user", "steps", "assistant", "steps"],
  );
  // 上框：step 1 完整（思考+工具）+ step 2 的纯思考组（工具与子代理被拆走）
  assert.equal(splitMsgs[1].groups.length, 2);
  assert.equal(splitMsgs[1].groups[0].step.index, 1);
  assert.equal(splitMsgs[1].groups[0].step.tools.length, 1);
  assert.equal(splitMsgs[1].groups[0].thoughts.length, 1);
  assert.equal(splitMsgs[1].groups[1].step.index, 2);
  assert.equal(splitMsgs[1].groups[1].step.tools.length, 0);
  assert.equal(splitMsgs[1].groups[1].step.subagents.length, 0);
  assert.equal(splitMsgs[1].groups[1].thoughts[0].text, "决定并行调用子代理");
  assert.equal(splitMsgs[2].content, "现在并行执行审核：");
  // 下框：step 2 的纯工具组（思考被拆走，子代理保留——只在此处渲染）
  assert.equal(splitMsgs[3].groups.length, 1);
  assert.equal(splitMsgs[3].groups[0].step.index, 2);
  assert.equal(splitMsgs[3].groups[0].step.tools.length, 1);
  assert.equal(splitMsgs[3].groups[0].step.subagents.length, 1);
  assert.equal(splitMsgs[3].groups[0].thoughts.length, 0);

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

  // Regression: a running NEW turn must not fall back to the previous
  // turn's session-level conclusion (it leaked below the new user bubble).
  const multiTurnSession = {
    ...baseSession,
    status: "running",
    conclusion: "上一轮结论",
    turns: [
      {
        message: { role: "user", text: "第一轮", timestamp: 1 },
        steps: [],
        conclusion: "上一轮结论",
      },
      {
        message: { role: "user", text: "第二轮", timestamp: 2 },
        steps: [],
      },
    ],
  };
  const multiMsgs = buildChatMessages(multiTurnSession, []);
  assert.deepEqual(
    multiMsgs.map((m) => m.type),
    ["user", "assistant", "user", "assistant"],
  );
  assert.equal(multiMsgs[1].content, "上一轮结论");
  assert.equal(multiMsgs[3].content, "");

  // Compaction notices render after the matching turn's process block
  const compactedSession = {
    ...baseSession,
    turns: [
      {
        message: { role: "user", text: "压缩测试", timestamp: 1 },
        steps: [
          {
            index: 1,
            numeral: "1",
            label: "a",
            skill: "",
            tools: [],
            turnIndex: 0,
          },
        ],
      },
    ],
    compactions: [
      { text: "12 条消息 → 3 条", turnIndex: 1, timestamp: 2 },
    ],
  };
  const compactedMsgs = buildChatMessages(compactedSession, []);
  assert.deepEqual(
    compactedMsgs.map((m) => m.type),
    ["user", "steps", "compacted"],
  );
  assert.equal(compactedMsgs[2].text, "12 条消息 → 3 条");

  // In-progress compaction renders a pending indicator at the tail of the
  // running turn only; a finished session never shows it.
  const compactingSession = {
    ...baseSession,
    status: "running",
    compacting: true,
    turns: [
      {
        message: { role: "user", text: "压缩中测试", timestamp: 1 },
        steps: [
          {
            index: 1,
            numeral: "1",
            label: "a",
            skill: "",
            tools: [],
            turnIndex: 0,
          },
        ],
      },
    ],
  };
  const compactingMsgs = buildChatMessages(compactingSession, []);
  const pendingMsg = compactingMsgs.find((m) => m.type === "compacted");
  assert.ok(pendingMsg);
  assert.equal(pendingMsg.pending, true);
  assert.equal(pendingMsg.text, "正在压缩上下文…");
  // 指示条在过程块（steps）之后
  const stepsIdx = compactingMsgs.findIndex((m) => m.type === "steps");
  assert.ok(compactingMsgs.indexOf(pendingMsg) > stepsIdx);

  const doneCompactingSession = { ...compactingSession, status: "completed" };
  const doneMsgs = buildChatMessages(doneCompactingSession, []);
  assert.ok(!doneMsgs.some((m) => m.type === "compacted" && m.pending));

  // Guard events become guard chat items in the last turn
  const guardSession = {
    ...baseSession,
    turns: [
      {
        message: { role: "user", text: "护栏测试", timestamp: 1 },
        steps: [],
      },
    ],
    guardEvents: [
      { type: "guard_triggered", layer: "input", guardName: "SensitiveInputGuard", action: "block", reason: "敏感内容" },
      { type: "guard_triggered", layer: "output", guardName: "PolicyGuard", action: "log" },
    ],
  };
  const guardMsgs = buildChatMessages(guardSession, []);
  assert.equal(guardMsgs.length, 3);
  assert.equal(guardMsgs[0].type, "user");
  assert.equal(guardMsgs[1].type, "guard");
  assert.equal(guardMsgs[1].guardName, "SensitiveInputGuard");
  assert.equal(guardMsgs[1].action, "block");
  assert.equal(guardMsgs[1].layer, "input");
  assert.equal(guardMsgs[2].type, "guard");
  assert.equal(guardMsgs[2].action, "log");

  // 运行中轮次：pendingVerdict 渲染在"边界之后 step"的工具框之前——
  // 文本是它预告的工具调用的导语（边界=文本开始流式时已有的 step 数）
  const mkStep = (index, name, status, verdict) => ({
    index,
    numeral: String(index),
    label: name,
    skill: "",
    tools: [
      {
        id: `tool-${index}`,
        name,
        displayName: null,
        skill: "",
        status,
      },
    ],
    turnIndex: 1,
    ...(verdict ? { verdict } : {}),
  });
  const pendingSession = {
    ...baseSession,
    status: "running",
    turns: [
      {
        message: { role: "user", text: "审核", timestamp: 1 },
        steps: [
          mkStep(1, "parse_document", "done"),
          mkStep(2, "content_audit", "running"),
        ],
      },
    ],
    pendingVerdict: "现在并行执行审核：",
    pendingVerdictAfterStepIndex: 1,
  };
  const pendingMsgs = buildChatMessages(pendingSession, []);
  assert.deepEqual(
    pendingMsgs.map((m) => m.type),
    ["user", "steps", "assistant", "steps", "assistant"],
  );
  assert.equal(pendingMsgs[1].groups[0].step.index, 1);
  assert.equal(pendingMsgs[1].isRunning, false);
  assert.equal(pendingMsgs[2].content, "现在并行执行审核：");
  assert.equal(pendingMsgs[3].groups[0].step.index, 2);
  assert.equal(pendingMsgs[3].isRunning, true); // 仅最后一框可处于流式中
  assert.equal(pendingMsgs[4].content, ""); // 运行中的结论占位

  // 同一文本经 step_verdict 转正后布局一致（无重复渲染、无跳变）
  const finalizedSession = {
    ...pendingSession,
    turns: [
      {
        message: { role: "user", text: "审核", timestamp: 1 },
        steps: [
          mkStep(1, "parse_document", "done"),
          mkStep(2, "content_audit", "done", "现在并行执行审核："),
        ],
      },
    ],
    pendingVerdict: "",
    pendingVerdictAfterStepIndex: 0,
  };
  const finalizedMsgs = buildChatMessages(finalizedSession, []);
  assert.deepEqual(
    finalizedMsgs.map((m) => m.type),
    ["user", "steps", "assistant", "steps", "assistant"],
  );
  assert.equal(finalizedMsgs[1].groups[0].step.index, 1);
  assert.equal(finalizedMsgs[2].content, "现在并行执行审核：");
  assert.equal(finalizedMsgs[3].groups[0].step.index, 2);
  assert.equal(finalizedMsgs[4].content, "");

  // 开场文本：尚无 step 时 pending 渲染在所有框之后（未来新框的上方）
  const openingSession = {
    ...baseSession,
    status: "running",
    turns: [
      {
        message: { role: "user", text: "审核", timestamp: 1 },
        steps: [],
      },
    ],
    pendingVerdict: "我将对文档进行完整的政府公文审核。",
    pendingVerdictAfterStepIndex: 0,
  };
  const openingMsgs = buildChatMessages(openingSession, []);
  assert.deepEqual(
    openingMsgs.map((m) => m.type),
    ["user", "assistant", "assistant"],
  );
  assert.equal(openingMsgs[1].content, "我将对文档进行完整的政府公文审核。");
  assert.equal(openingMsgs[2].content, "");

  // 流式 pending 拆分：当前 think 的思考随上框，待定文本之后，工具组开新框
  const pendingSplitSession = {
    ...pendingSession,
    turns: [
      {
        message: { role: "user", text: "审核", timestamp: 1 },
        steps: [
          mkStep(1, "parse_document", "done"),
          mkStep(2, "content_audit", "running"),
        ],
      },
    ],
    thoughts: [
      { id: 1, text: "先解析文档", turn: 1, turnIndex: 1, stepIndex: 1, timestamp: 1 },
      {
        id: 2,
        text: "决定并行调用子代理",
        turn: 2,
        turnIndex: 1,
        stepIndex: 2,
        timestamp: 2,
      },
    ],
    pendingVerdict: "现在并行执行审核：",
    pendingVerdictAfterStepIndex: 1,
  };
  const pendingSplitMsgs = buildChatMessages(pendingSplitSession, []);
  assert.deepEqual(
    pendingSplitMsgs.map((m) => m.type),
    ["user", "steps", "assistant", "steps", "assistant"],
  );
  assert.equal(pendingSplitMsgs[1].groups.length, 2);
  assert.equal(pendingSplitMsgs[1].groups[0].step.index, 1);
  assert.equal(pendingSplitMsgs[1].groups[0].step.tools.length, 1);
  assert.equal(pendingSplitMsgs[1].groups[1].step.index, 2);
  assert.equal(pendingSplitMsgs[1].groups[1].step.tools.length, 0);
  assert.equal(
    pendingSplitMsgs[1].groups[1].thoughts[0].text,
    "决定并行调用子代理",
  );
  assert.equal(pendingSplitMsgs[2].content, "现在并行执行审核：");
  assert.equal(pendingSplitMsgs[3].groups.length, 1);
  assert.equal(pendingSplitMsgs[3].groups[0].step.index, 2);
  assert.equal(pendingSplitMsgs[3].groups[0].step.tools.length, 1);
  assert.equal(pendingSplitMsgs[3].groups[0].thoughts.length, 0);
  assert.equal(pendingSplitMsgs[3].isRunning, true);
  assert.equal(pendingSplitMsgs[4].content, "");

  // Citations merge across all search_documents calls of a turn, in call
  // order — the backend numbers hits cumulatively across calls.
  const citeHit = (title) => ({ title });
  const multiSearchTurn = {
    message: { role: "user", text: "查询", timestamp: 1 },
    steps: [
      {
        index: 1,
        numeral: "1",
        label: "step",
        skill: "s",
        turnIndex: 0,
        tools: [
          {
            id: "t1",
            name: "search_documents",
            skill: "",
            status: "done",
            callKind: "tool",
            callScope: "parent",
            subagentName: null,
            citations: [citeHit("文档A"), citeHit("文档B")],
          },
          {
            id: "t2",
            name: "search_documents",
            skill: "",
            status: "done",
            callKind: "tool",
            callScope: "parent",
            subagentName: null,
            citations: [citeHit("文档C")],
          },
        ],
      },
    ],
    conclusion: "见[[3]]",
  };
  const mergedMsgs = buildChatMessages(
    { ...baseSession, turns: [multiSearchTurn] },
    [],
  );
  const assistantMsg = mergedMsgs.find((m) => m.type === "assistant");
  // Legacy events (no offsets): merge-order list + sequential numbering.
  assert.equal(assistantMsg.citations.list.length, 3);
  assert.equal(assistantMsg.citations.list[0].title, "文档A");
  assert.equal(assistantMsg.citations.list[1].title, "文档B");
  assert.equal(assistantMsg.citations.list[2].title, "文档C");
  assert.equal(assistantMsg.citations.byNumber.get(3).title, "文档C");

  console.log("chatMessages verification passed");
} finally {
  rmSync(outDir, { recursive: true, force: true });
}
