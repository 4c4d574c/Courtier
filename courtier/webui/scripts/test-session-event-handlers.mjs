import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { rmSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const scriptDir = dirname(fileURLToPath(import.meta.url));
const rootDir = resolve(scriptDir, "..");
const outDir = resolve(rootDir, ".tmp/session-event-handlers-test");

rmSync(outDir, { recursive: true, force: true });
mkdirSync(outDir, { recursive: true });

try {
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

  const mod = await import(
    pathToFileURL(resolve(outDir, "sessionEventHandlers.bundle.js")).href
  );
  const { createSessionEventHandlers, finalizeRunningOperations } = mod;

  function makeDeps() {
    const session = {
      id: "",
      task: "审核文档",
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
    const handlers = createSessionEventHandlers(() => ({
      state,
      session,
    }));
    return { handlers, state, session };
  }

  // Basic step and tool lifecycle
  {
    const { handlers, session } = makeDeps();
    handlers.handleSessionEvent({
      type: "think",
      detail: "tool_calls:parse_document",
    });
    assert.equal(session.steps.length, 1);
    assert.equal(session.steps[0].tools.length, 1);
    assert.equal(session.steps[0].tools[0].name, "parse_document");
    assert.equal(session.steps[0].tools[0].status, "pending");

    handlers.handleSessionEvent({
      type: "act",
      detail: "executing:parse_document",
    });
    assert.equal(session.steps[0].tools[0].status, "running");

    handlers.handleSessionEvent({
      type: "tool_result",
      name: "parse_document",
      status: "ok",
      callKind: "tool",
      callScope: "parent",
      summary: "parsed",
    });
    assert.equal(session.steps[0].tools[0].status, "done");
    assert.equal(session.steps[0].tools[0].summary, "parsed");
  }

  // tool_result with issueCounts lands on the tool record
  {
    const { handlers, session } = makeDeps();
    handlers.handleSessionEvent({
      type: "think",
      detail: "tool_calls:check_format",
    });
    const counts = { err: 3, warn: 0, ok: 1, unchecked: 2 };
    handlers.handleSessionEvent({
      type: "tool_result",
      name: "check_format",
      status: "ok",
      callKind: "tool",
      callScope: "parent",
      summary: "发现 3 处错误",
      issueCounts: counts,
    });
    assert.deepEqual(session.steps[0].tools[0].issueCounts, counts);
  }

  // subagent_tool_result with issueCounts lands on the sub-agent tool record
  {
    const { handlers, session } = makeDeps();
    handlers.handleSessionEvent({
      type: "think",
      detail: "tool_calls:format_audit",
    });
    handlers.handleSessionEvent({
      type: "subagent_start",
      name: "format_audit",
      handleId: "fmt-1",
      task: "格式审核",
    });
    const counts = { err: 1, warn: 2, ok: 7 };
    handlers.handleSessionEvent({
      type: "subagent_tool_result",
      name: "format_audit",
      handleId: "fmt-1",
      toolName: "check_format",
      toolStatus: "ok",
      toolSummary: "checked",
      issueCounts: counts,
    });
    const step = session.steps[session.steps.length - 1];
    assert.deepEqual(step.subagents[0].tools[0].issueCounts, counts);
  }

  // subagent_run wrapper tool_result carries issueCounts onto the wrapper
  {
    const { handlers, session } = makeDeps();
    handlers.handleSessionEvent({
      type: "think",
      detail: "tool_calls:format_audit",
    });
    handlers.handleSessionEvent({
      type: "subagent_start",
      name: "format_audit",
      handleId: "fmt-1",
      task: "格式审核",
    });
    handlers.handleSessionEvent({
      type: "subagent_end",
      name: "format_audit",
      handleId: "fmt-1",
      result: { status: "completed" },
    });
    const counts = { err: 0, warn: 1, ok: 9, unchecked: 4 };
    handlers.handleSessionEvent({
      type: "tool_result",
      name: "format_audit",
      status: "ok",
      callKind: "subagent_run",
      callScope: "parent",
      handleId: "fmt-1",
      summary: "audit complete",
      issueCounts: counts,
    });
    const step = session.steps[session.steps.length - 1];
    assert.deepEqual(step.subagents[0].wrapper.issueCounts, counts);
  }

  // Sub-agent lifecycle with nested child
  {
    const { handlers, session } = makeDeps();
    handlers.handleSessionEvent({
      type: "think",
      detail: "tool_calls:full_government_audit",
    });
    handlers.handleSessionEvent({
      type: "subagent_start",
      name: "full_government_audit",
      handleId: "fg-1",
      task: "完整审核",
    });
    let step = session.steps[session.steps.length - 1];
    assert.equal(step.subagents.length, 1);
    assert.equal(step.subagents[0].name, "full_government_audit");
    assert.equal(step.subagents[0].status, "running");

    handlers.handleSessionEvent({
      type: "subagent_token",
      name: "full_government_audit",
      handleId: "fg-1",
      text: "planning",
    });
    step = session.steps[session.steps.length - 1];
    assert.equal(step.subagents[0].thoughts.length, 1);
    assert.equal(step.subagents[0].thoughts[0].text, "planning");

    handlers.handleSessionEvent({
      type: "subagent_think",
      name: "full_government_audit",
      handleId: "fg-1",
      text: "text_response",
    });
    step = session.steps[session.steps.length - 1];
    assert.equal(step.subagents[0].thoughts.length, 2);

    handlers.handleSessionEvent({
      type: "subagent_think",
      name: "full_government_audit",
      handleId: "fg-1",
      text: "reasoning",
    });
    step = session.steps[session.steps.length - 1];
    assert.equal(step.subagents[0].thoughts.length, 2);
    assert.ok(step.subagents[0].thoughts[1].text.endsWith("reasoning"));

    handlers.handleSessionEvent({
      type: "subagent_start",
      name: "format_audit",
      handleId: "fmt-1",
      parentHandleId: "fg-1",
      task: "格式审核",
    });
    step = session.steps[session.steps.length - 1];
    assert.equal(step.subagents[0].children.length, 1);
    assert.equal(step.subagents[0].children[0].name, "format_audit");

    handlers.handleSessionEvent({
      type: "subagent_tool_result",
      name: "format_audit",
      handleId: "fmt-1",
      parentHandleId: "fg-1",
      toolName: "check_font",
      toolStatus: "ok",
      toolSummary: "font ok",
    });
    step = session.steps[session.steps.length - 1];
    assert.equal(step.subagents[0].children[0].tools.length, 1);
    assert.equal(step.subagents[0].children[0].tools[0].name, "check_font");

    handlers.handleSessionEvent({
      type: "subagent_end",
      name: "format_audit",
      handleId: "fmt-1",
      parentHandleId: "fg-1",
      result: { status: "completed" },
    });
    step = session.steps[session.steps.length - 1];
    assert.equal(step.subagents[0].children[0].status, "completed");

    handlers.handleSessionEvent({
      type: "subagent_end",
      name: "full_government_audit",
      handleId: "fg-1",
      result: { status: "completed" },
    });
    step = session.steps[session.steps.length - 1];
    assert.equal(step.subagents[0].status, "completed");

    handlers.handleSessionEvent({
      type: "tool_result",
      name: "full_government_audit",
      status: "ok",
      callKind: "subagent_run",
      callScope: "parent",
      handleId: "fg-1",
      summary: "audit complete",
    });
    step = session.steps[session.steps.length - 1];
    assert.equal(step.subagents[0].wrapper.summary, "audit complete");
  }

  // Context compaction events append a notice tied to the current turn
  {
    const { handlers, session, state } = makeDeps();
    state.currentTurnIndex = 1;
    handlers.handleSessionEvent({
      type: "context_compacted",
      detail: "12 条消息 → 3 条",
    });
    handlers.handleSessionEvent({ type: "context_compacted" });
    assert.equal(session.compactions.length, 2);
    assert.equal(session.compactions[0].text, "12 条消息 → 3 条");
    assert.equal(session.compactions[0].turnIndex, 1);
    assert.equal(session.compactions[1].text, "上下文已压缩");
  }

  // context_compacting sets the in-progress flag; context_compacted clears
  // it and appends the notice.
  {
    const { handlers, session, state } = makeDeps();
    state.currentTurnIndex = 1;
    handlers.handleSessionEvent({ type: "context_compacting" });
    assert.equal(session.compacting, true);
    handlers.handleSessionEvent({
      type: "context_compacted",
      detail: "12 条消息 → 3 条",
    });
    assert.equal(session.compacting, false);
    assert.equal(session.compactions.length, 1);
    assert.equal(session.compactions[0].text, "12 条消息 → 3 条");
  }

  // Regression: top-level sub-agent whose parentHandleId is the orchestrator
  // root handle (not a node in the tree) — its tool results must attach at
  // the top level instead of being dropped.
  {
    const { handlers, session } = makeDeps();
    handlers.handleSessionEvent({
      type: "think",
      detail: "tool_calls:content_audit",
    });
    handlers.handleSessionEvent({
      type: "subagent_start",
      name: "content_audit",
      handleId: "ca-1",
      parentHandleId: "orch-root-1",
      task: "内容审核",
    });
    handlers.handleSessionEvent({
      type: "subagent_tool_result",
      name: "content_audit",
      handleId: "ca-1",
      parentHandleId: "orch-root-1",
      toolName: "list_artifacts",
      toolStatus: "ok",
      toolSummary: "2 artifacts",
    });
    handlers.handleSessionEvent({
      type: "subagent_tool_result",
      name: "content_audit",
      handleId: "ca-1",
      parentHandleId: "orch-root-1",
      toolName: "get_artifact",
      toolStatus: "ok",
      toolSummary: "document",
    });
    const step = session.steps[session.steps.length - 1];
    assert.equal(step.subagents.length, 1);
    assert.equal(step.subagents[0].name, "content_audit");
    assert.equal(step.subagents[0].tools.length, 2);
    assert.equal(step.subagents[0].tools[0].name, "list_artifacts");
    assert.equal(step.subagents[0].tools[1].name, "get_artifact");
  }

  // Sub-agent error propagated to wrapper
  {
    const { handlers, session } = makeDeps();
    handlers.handleSessionEvent({
      type: "think",
      detail: "tool_calls:full_government_audit",
    });
    handlers.handleSessionEvent({
      type: "subagent_start",
      name: "full_government_audit",
      handleId: "fg-err",
      task: "完整审核",
    });
    handlers.handleSessionEvent({
      type: "subagent_end",
      name: "full_government_audit",
      handleId: "fg-err",
      result: { status: "error", error: "subagent failed" },
    });
    const step = session.steps[session.steps.length - 1];
    assert.equal(step.subagents[0].status, "error");
    assert.equal(step.subagents[0].error, "subagent failed");
    assert.equal(step.tools[0].status, "error");
    assert.equal(step.tools[0].summary, "subagent failed");
  }

  // Duplicate root-level subagent invocations keep separate wrappers
  {
    const { handlers, session } = makeDeps();
    handlers.handleSessionEvent({
      type: "think",
      detail: "tool_calls:format_audit,format_audit",
    });
    handlers.handleSessionEvent({
      type: "subagent_start",
      name: "format_audit",
      handleId: "fmt-1",
      task: "first",
    });
    handlers.handleSessionEvent({
      type: "subagent_start",
      name: "format_audit",
      handleId: "fmt-2",
      task: "second",
    });
    handlers.handleSessionEvent({
      type: "tool_result",
      name: "format_audit",
      status: "ok",
      callKind: "subagent_run",
      callScope: "parent",
      handleId: "fmt-1",
      summary: "first done",
    });
    handlers.handleSessionEvent({
      type: "tool_result",
      name: "format_audit",
      status: "ok",
      callKind: "subagent_run",
      callScope: "parent",
      handleId: "fmt-2",
      summary: "second done",
    });
    const step = session.steps[session.steps.length - 1];
    assert.equal(step.tools.length, 2);
    assert.equal(step.tools[0].summary, "first done");
    assert.equal(step.tools[1].summary, "second done");
    assert.equal(step.subagents.length, 2);
    assert.equal(step.subagents[0].wrapper.summary, "first done");
    assert.equal(step.subagents[1].wrapper.summary, "second done");
  }

  // Error event finalizes running/pending tools and sub-agents
  {
    const { handlers, session } = makeDeps();
    handlers.handleSessionEvent({
      type: "think",
      detail: "tool_calls:parse_document,check_format",
    });
    handlers.handleSessionEvent({
      type: "act",
      detail: "executing:parse_document,check_format",
    });
    const startTime = Date.now() - 1200;
    session.steps[0].tools[0].startTime = startTime;

    handlers.handleSessionEvent({ type: "error", detail: "引擎异常" });
    assert.equal(session.status, "error");
    assert.equal(session.steps[0].tools[0].status, "error");
    assert.equal(session.steps[0].tools[0].summary, "异常结束");
    assert.ok(session.steps[0].tools[0].duration >= 1.1);
    assert.equal(session.steps[0].tools[1].status, "error");
  }

  // Error event finalizes running sub-agent trees and their tools
  {
    const { handlers, session } = makeDeps();
    handlers.handleSessionEvent({
      type: "think",
      detail: "tool_calls:full_government_audit",
    });
    handlers.handleSessionEvent({
      type: "subagent_start",
      name: "full_government_audit",
      handleId: "fg-1",
      task: "完整审核",
    });
    handlers.handleSessionEvent({
      type: "subagent_start",
      name: "format_audit",
      handleId: "fmt-1",
      parentHandleId: "fg-1",
      task: "格式审核",
    });
    handlers.handleSessionEvent({
      type: "subagent_tool_result",
      name: "format_audit",
      handleId: "fmt-1",
      parentHandleId: "fg-1",
      toolName: "check_font",
      toolStatus: "running",
      toolSummary: "checking",
    });

    handlers.handleSessionEvent({ type: "error", detail: "引擎异常" });
    const root = session.steps[0].subagents[0];
    assert.equal(root.status, "error");
    assert.equal(root.error, "异常结束");
    assert.equal(root.children[0].status, "error");
    assert.equal(root.children[0].tools[0].status, "error");
    assert.equal(session.steps[0].tools[0].status, "error");
  }

  // Stopped event marks tools as cancelled
  {
    const { handlers, session } = makeDeps();
    handlers.handleSessionEvent({
      type: "think",
      detail: "tool_calls:parse_document",
    });
    handlers.handleSessionEvent({
      type: "act",
      detail: "executing:parse_document",
    });

    handlers.handleSessionEvent({ type: "stopped" });
    assert.equal(session.steps[0].tools[0].status, "cancelled");
  }

  // New Pi-architecture events are captured on session
  {
    const { handlers, session } = makeDeps();
    handlers.handleSessionEvent({
      type: "guard_triggered",
      layer: "input",
      guardName: "SensitiveInputGuard",
      action: "block",
      reason: "敏感内容",
    });
    assert.equal(session.guardEvents?.length, 1);
    assert.equal(session.guardEvents[0].action, "block");

    handlers.handleSessionEvent({
      type: "hint_injected",
      hintType: "terminal_ready",
      text: "业务工具已就绪",
    });
    assert.equal(session.hintEvents?.length, 1);
    assert.equal(session.hintEvents[0].hintType, "terminal_ready");

    handlers.handleSessionEvent({
      type: "model_selected",
      model: "qwen3.6-27b",
      backend: "openai",
      strategy: "primary",
    });
    handlers.handleSessionEvent({
      type: "model_fallback",
      model: "qwen3.6-27b",
      backend: "local_backup",
      reason: "timeout",
    });
    assert.equal(session.modelEvents?.length, 2);
    assert.equal(session.modelEvents[1].type, "model_fallback");
    assert.equal(session.modelEvents[1].backend, "local_backup");

    handlers.handleSessionEvent({
      type: "loop_completed",
      status: "completed",
      terminationReason: "max_steps",
      totalSteps: 5,
    });
    assert.equal(session.loopCompleted?.totalSteps, 5);
  }

  // step_verdict / conclusion_token / complete：中间结论文本的归属语义
  {
    const { handlers, state, session } = makeDeps();
    const turn = {
      message: { role: "user", text: "审核", timestamp: 1 },
      steps: [],
    };
    session.turns.push(turn);
    state.currentTurn = turn;
    state.currentTurnIndex = 1;

    handlers.handleSessionEvent({
      type: "think",
      detail: "tool_calls:parse_document",
    });
    assert.equal(session.steps.length, 1);

    // 中间文本流式到达：只进 pendingVerdict 缓冲，不进结论；
    // 首个 token 记录归属边界（= 当前 think 的 step 的前一个）
    handlers.handleSessionEvent({
      type: "conclusion_token",
      text: "文档已解析，",
    });
    handlers.handleSessionEvent({ type: "conclusion_token", text: "共1页。" });
    assert.equal(session.pendingVerdict, "文档已解析，共1页。");
    assert.equal(session.pendingVerdictAfterStepIndex, 0);
    assert.equal(turn.conclusion, undefined);

    // observe 时后端发 step_verdict：转正到 step 1（两个数组同步），清空缓冲
    handlers.handleSessionEvent({
      type: "step_verdict",
      stepIndex: 1,
      text: "文档已解析，共1页。",
    });
    assert.equal(session.steps[0].verdict, "文档已解析，共1页。");
    assert.equal(session.turns[0].steps[0].verdict, "文档已解析，共1页。");
    assert.equal(session.pendingVerdict, "");
    assert.equal(session.pendingVerdictAfterStepIndex, 0);
    assert.equal(turn.conclusion, undefined);

    // 最终结论文本 + complete：流式剩余缓冲优先（complete 全量仅兜底）
    handlers.handleSessionEvent({
      type: "conclusion_token",
      text: "审核完成，文档合规。",
    });
    assert.equal(session.pendingVerdictAfterStepIndex, 0);
    handlers.handleSessionEvent({
      type: "complete",
      conclusion: "文档已解析，共1页。审核完成，文档合规。",
      tokensIn: 10,
      tokensOut: 5,
    });
    assert.equal(turn.conclusion, "审核完成，文档合规。");
    assert.equal(session.pendingVerdict, "");
    assert.equal(session.pendingVerdictAfterStepIndex, 0);
    assert.equal(session.conclusion, "文档已解析，共1页。审核完成，文档合规。");
  }

  // 流式真实顺序：text_response 占位 step → conclusion_token → tool_calls
  // 补丁到占位 step。待定文本的边界必须取占位 step 之前，否则文本会被
  // 推到占位 step（未来工具框）之后，observe 转正时发生跳变。
  {
    const { handlers, state, session } = makeDeps();
    const turn = {
      message: { role: "user", text: "审核", timestamp: 1 },
      steps: [],
    };
    session.turns.push(turn);
    state.currentTurn = turn;
    state.currentTurnIndex = 1;

    // 第一轮 think：占位 step 1 → 文本 → 工具补丁
    handlers.handleSessionEvent({ type: "think", detail: "text_response" });
    assert.equal(session.steps.length, 1);
    assert.equal(session.steps[0].tools.length, 0);
    handlers.handleSessionEvent({ type: "conclusion_token", text: "先解析文档。" });
    assert.equal(session.pendingVerdictAfterStepIndex, 0); // 渲染在 step 1 框之前
    handlers.handleSessionEvent({
      type: "think",
      detail: "tool_calls:parse_document",
    });
    assert.equal(session.steps.length, 1); // 补丁到占位 step，不新建
    handlers.handleSessionEvent({
      type: "step_verdict",
      stepIndex: 1,
      text: "先解析文档。",
    });
    assert.equal(session.pendingVerdict, "");

    // observe 后第二轮 think：占位 step 2 → 文本边界 = 1（step 1 之后、
    // step 2 的工具框之前）
    handlers.handleSessionEvent({ type: "observe" });
    handlers.handleSessionEvent({ type: "think", detail: "text_response" });
    assert.equal(session.steps.length, 2);
    handlers.handleSessionEvent({
      type: "conclusion_token",
      text: "现在并行执行审核：",
    });
    assert.equal(session.pendingVerdictAfterStepIndex, 1);
    handlers.handleSessionEvent({
      type: "think",
      detail: "tool_calls:content_audit",
    });
    assert.equal(session.steps.length, 2); // 仍补丁到占位 step 2
  }

  // complete 兜底：无流式 token 时落 complete 事件的结论
  {
    const { handlers, state, session } = makeDeps();
    const turn = {
      message: { role: "user", text: "审核", timestamp: 1 },
      steps: [],
    };
    session.turns.push(turn);
    state.currentTurn = turn;
    handlers.handleSessionEvent({ type: "complete", conclusion: "完整结论" });
    assert.equal(turn.conclusion, "完整结论");
  }

  // step_verdict 指向未知 index：回退到最后一个 step
  {
    const { handlers, state, session } = makeDeps();
    const turn = {
      message: { role: "user", text: "审核", timestamp: 1 },
      steps: [],
    };
    session.turns.push(turn);
    state.currentTurn = turn;
    handlers.handleSessionEvent({
      type: "think",
      detail: "tool_calls:check_format",
    });
    handlers.handleSessionEvent({ type: "conclusion_token", text: "中间文本" });
    handlers.handleSessionEvent({
      type: "step_verdict",
      stepIndex: 99,
      text: "中间文本",
    });
    assert.equal(session.steps[0].verdict, "中间文本");
    assert.equal(session.pendingVerdict, "");
  }

  // stopped：缓冲中的未定性文本落到结论而不是丢失
  {
    const { handlers, state, session } = makeDeps();
    const turn = {
      message: { role: "user", text: "审核", timestamp: 1 },
      steps: [],
    };
    session.turns.push(turn);
    state.currentTurn = turn;
    handlers.handleSessionEvent({
      type: "think",
      detail: "tool_calls:parse_document",
    });
    handlers.handleSessionEvent({ type: "conclusion_token", text: "写到一半" });
    assert.equal(session.pendingVerdictAfterStepIndex, 0);
    handlers.handleSessionEvent({ type: "stopped" });
    assert.equal(turn.conclusion, "写到一半");
    assert.equal(session.pendingVerdict, "");
    assert.equal(session.pendingVerdictAfterStepIndex, 0);
  }

  console.log("sessionEventHandlers verification passed");
} finally {
  rmSync(outDir, { recursive: true, force: true });
}
