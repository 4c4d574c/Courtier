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
    const turnVersion = { value: 0 };
    const handlers = createSessionEventHandlers(() => ({
      state,
      session,
      turnVersion,
    }));
    return { handlers, state, session, turnVersion };
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

  console.log("sessionEventHandlers verification passed");
} finally {
  rmSync(outDir, { recursive: true, force: true });
}
