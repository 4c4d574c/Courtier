import type {
  Session,
  Step,
  AgentEvent,
  ToolResult,
  ToolStatus,
  ToolDetail,
  Turn,
  SubagentRun,
} from "../types/agent";
import { normalizeToolResult } from "../utils/toolCalls";
import { toChineseNumeral } from "../utils/chineseNumerals";
import type { Ref } from "vue";
import {
  addSubagentToolInContainer,
  appendSubagentThoughtInContainer,
  findRunningParentByHandleId,
  newSubagentThoughtBlockInContainer,
  routeSubagentWrapper,
  type PendingSubagent,
  updateNodeInTree,
  upsertNodeInTree,
} from "./subagentTree";

// ---------------------------------------------------------------------------
// Runtime type guards
// ---------------------------------------------------------------------------
const VALID_TOOL_STATUSES: readonly string[] = [
  "pending",
  "running",
  "done",
  "error",
  "warning",
  "cancelled",
];

function asToolStatus(
  s: string | undefined,
  fallback: ToolStatus = "done",
): ToolStatus {
  if (s === "ok") return "done";
  if (s && VALID_TOOL_STATUSES.includes(s)) return s as ToolStatus;
  return fallback;
}

function isToolDetail(v: unknown): v is ToolDetail {
  if (!v || typeof v !== "object") return false;
  const d = v as Record<string, unknown>;
  return d.type === "structured" || d.type === "markdown";
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

// ---------------------------------------------------------------------------
// Finalize running/pending operations when the session ends abnormally
// ---------------------------------------------------------------------------

export function finalizeRunningOperations(
  session: Session,
  toolStatus: ToolStatus = "error",
  runStatus: SubagentRun["status"] = "error",
  summary?: string,
): void {
  const now = Date.now();

  function finalizeTool(tool: ToolResult): ToolResult {
    if (tool.status !== "running" && tool.status !== "pending") return tool;
    const duration =
      tool.status === "running" && tool.startTime
        ? (now - tool.startTime) / 1000
        : tool.duration;
    return {
      ...tool,
      status: toolStatus,
      duration,
      summary: summary ?? tool.summary,
    };
  }

  function finalizeRun(run: SubagentRun): SubagentRun {
    const statusChanged = run.status === "running";
    return {
      ...run,
      status: statusChanged ? runStatus : run.status,
      error: statusChanged && summary ? summary : run.error,
      tools: run.tools?.map(finalizeTool),
      wrapper: run.wrapper ? finalizeTool(run.wrapper) : undefined,
      children: run.children?.map(finalizeRun),
    };
  }

  for (let i = 0; i < session.steps.length; i++) {
    const step = session.steps[i];
    const tools = step.tools.map(finalizeTool);
    const subagents = step.subagents?.map(finalizeRun);
    const newStep: Step = { ...step, tools, subagents };
    session.steps[i] = newStep;
    if (step.turnIndex !== undefined) {
      const turn = session.turns[step.turnIndex - 1];
      if (turn) {
        const idx = turn.steps.findIndex((st) => st.index === step.index);
        if (idx >= 0) {
          turn.steps[idx] = newStep;
        }
      }
    }
  }
}

// ---------------------------------------------------------------------------
// Mutable state shared between composable and handler
// ---------------------------------------------------------------------------
export interface MutableState {
  currentTurn: Turn | null;
  currentTurnIndex: number;
  currentStepIndex: number;
  segmentIndex: number;
  currentSegmentType: "observe" | "tool_result";
  currentThoughtTurn: number;
  observedSinceLastStep: boolean;
  thoughtIdCounter: number;
  toolIdCounter: number;
  /** Sub-agent thought id counters keyed by handle id. */
  subagentThoughtCounters: Record<string, number>;
  /** Sub-agent start events that arrived before the current step existed. */
  pendingSubagents: PendingSubagent[];
}

export interface HandlerDeps {
  state: MutableState;
  session: Session;
  turnVersion: Ref<number>;
}

export function createSessionEventHandlers(deps: () => HandlerDeps) {
  const d = () => deps();

  // ---- step lifecycle helpers ----

  function createStep(index: number, toolNames: string[]): Step {
    const { state: s } = d();
    const tools: ToolResult[] = toolNames.map((name) =>
      normalizeToolResult({
        id: `tool-${++s.toolIdCounter}`,
        name,
        skill: "",
        status: "pending",
      }),
    );
    return {
      index,
      numeral: toChineseNumeral(index),
      label: toolNames.join(", "),
      skill: "",
      tools,
      turnIndex: s.currentTurnIndex,
      startSegmentIndex: s.segmentIndex,
    };
  }

  function replaceStepAt(stepIndex: number, updatedStep: Step): void {
    const { session } = d();
    session.steps.splice(stepIndex, 1, updatedStep);
    const turn =
      updatedStep.turnIndex !== undefined
        ? session.turns[updatedStep.turnIndex - 1]
        : undefined;
    if (turn) {
      const turnStepIndex = turn.steps.findIndex(
        (st) => st.index === updatedStep.index,
      );
      if (turnStepIndex >= 0) {
        turn.steps.splice(turnStepIndex, 1, updatedStep);
      } else if (import.meta.env.DEV) {
        console.warn(
          `replaceStepAt: step ${updatedStep.index} not found in turn ${updatedStep.turnIndex}`,
        );
      }
    }
  }

  function patchLastStep(patch: Partial<Step>): void {
    const { session } = d();
    const lastIndex = session.steps.length - 1;
    const step = session.steps[lastIndex];
    if (!step) return;
    replaceStepAt(lastIndex, { ...step, ...patch });
  }

  function addStep(step: Step): boolean {
    const { state: s, session, turnVersion } = d();
    session.steps.push(step);
    if (s.currentTurn) {
      s.currentTurn.steps.push(step);
      turnVersion.value++;
      return true;
    }
    return false;
  }

  // ---- thought helpers ----

  function appendThought(text: string, source?: string): void {
    const { state: s, session } = d();
    const lastIndex = session.thoughts.length - 1;
    let currentThought = session.thoughts[lastIndex];
    if (!currentThought || currentThought.turn !== s.currentThoughtTurn) {
      s.currentThoughtTurn++;
      currentThought = {
        id: ++s.thoughtIdCounter,
        text,
        turn: s.currentThoughtTurn,
        turnIndex: s.currentTurnIndex,
        segmentIndex: s.segmentIndex,
        segmentType: s.currentSegmentType,
        stepIndex: source
          ? undefined
          : s.currentStepIndex > 0
            ? s.currentStepIndex
            : undefined,
        source,
        timestamp: Date.now(),
      };
      session.thoughts.push(currentThought);
    } else {
      session.thoughts[lastIndex] = {
        ...currentThought,
        text: currentThought.text + text,
      };
    }
  }

  function assignPendingThoughtsToCurrentStep(): void {
    const { state: s, session } = d();
    if (s.currentStepIndex <= 0) return;
    session.thoughts = session.thoughts.map((t) => {
      if (t.source || t.stepIndex !== undefined) return t;
      return { ...t, stepIndex: s.currentStepIndex };
    });
  }

  // ---- sub-agent helpers ----

  function nextSubagentThoughtId(handleId: string): number {
    const { state: s } = d();
    const next = (s.subagentThoughtCounters[handleId] ?? 0) + 1;
    s.subagentThoughtCounters[handleId] = next;
    return next;
  }

  function flushPendingSubagents(): void {
    const { state: s, session } = d();
    const step = session.steps[session.steps.length - 1];
    if (!step || s.pendingSubagents.length === 0) return;
    let roots = step.subagents ?? [];
    for (const { run, parentHandleId } of s.pendingSubagents) {
      roots = upsertNodeInTree(
        roots,
        run.name,
        run.handleId,
        parentHandleId,
        run,
      );
    }
    patchLastStep({ subagents: roots });
    s.pendingSubagents = [];
  }

  function upsertSubagentRun(
    name: string,
    handleId: string,
    patch: Partial<SubagentRun>,
    parentHandleId?: string | null,
  ): void {
    const { state: s, session } = d();
    const step = session.steps[session.steps.length - 1];
    if (!step) {
      s.pendingSubagents.push({
        run: {
          name,
          handleId,
          task: patch.task ?? "",
          status: patch.status ?? "running",
          ...patch,
        },
        parentHandleId,
      });
      return;
    }
    patchLastStep({
      subagents: upsertNodeInTree(
        step.subagents ?? [],
        name,
        handleId,
        parentHandleId,
        patch,
      ),
    });
  }

  function appendSubagentThought(
    handleId: string,
    text: string,
    parentHandleId?: string | null,
  ): void {
    const { session } = d();
    const step = session.steps[session.steps.length - 1];
    if (!step) return;
    const roots = step.subagents ?? [];
    const nextId = nextSubagentThoughtId(handleId);

    if (parentHandleId) {
      const parent = findRunningParentByHandleId(roots, parentHandleId);
      if (parent) {
        patchLastStep({
          subagents: updateNodeInTree(roots, parent.handleId, (p) => ({
            ...p,
            children: appendSubagentThoughtInContainer(
              p.children ?? [],
              handleId,
              text,
              nextId,
            ),
          })),
        });
        return;
      }
    }
    patchLastStep({
      subagents: appendSubagentThoughtInContainer(
        roots,
        handleId,
        text,
        nextId,
      ),
    });
  }

  function newSubagentThoughtBlock(
    handleId: string,
    parentHandleId?: string | null,
  ): void {
    const { session } = d();
    const step = session.steps[session.steps.length - 1];
    if (!step) return;
    const nextId = nextSubagentThoughtId(handleId);
    const roots = step.subagents ?? [];

    if (parentHandleId) {
      const parent = findRunningParentByHandleId(roots, parentHandleId);
      if (parent) {
        patchLastStep({
          subagents: updateNodeInTree(roots, parent.handleId, (p) => ({
            ...p,
            children: newSubagentThoughtBlockInContainer(
              p.children ?? [],
              handleId,
              nextId,
            ),
          })),
        });
        return;
      }
    }
    patchLastStep({
      subagents: newSubagentThoughtBlockInContainer(roots, handleId, nextId),
    });
  }

  function closeLastStepSegmentRange(): void {
    const { session } = d();
    const lastStep = session.steps[session.steps.length - 1];
    if (!lastStep || lastStep.endSegmentIndex != null) return;
    patchLastStep({ endSegmentIndex: d().state.segmentIndex });
  }

  function applyToolDetail(
    tool: ToolResult,
    rawDetail: ToolDetail | string | undefined,
  ): ToolResult {
    if (rawDetail && typeof rawDetail === "object" && "type" in rawDetail) {
      return { ...tool, detail: rawDetail as ToolDetail };
    }
    if (rawDetail) {
      return {
        ...tool,
        detail: { type: "markdown", content: String(rawDetail) } as ToolDetail,
      };
    }
    return tool;
  }

  function handleSubagentRunToolResult(
    event: AgentEvent,
    rawDetail: ToolDetail | string | undefined,
  ): void {
    const { state: s } = d();
    const status = asToolStatus(event.status, "done");
    const wrapper: ToolResult = applyToolDetail(
      normalizeToolResult({
        id: `tool-${++s.toolIdCounter}`,
        name: event.name!,
        skill: event.skill ?? "",
        skillDescription: event.skillDescription ?? "",
        status,
        callKind: event.callKind ?? "subagent_run",
        callScope: event.callScope ?? "parent",
        subagentName: event.subagentName ?? event.name!,
        handleId: event.handleId ?? null,
        parentHandleId: event.parentHandleId ?? null,
        summary: event.summary ?? "",
        duration: event.duration,
      }),
      rawDetail,
    );

    const subagentName = event.subagentName ?? event.name!;
    const handleId = event.handleId;
    const parentHandleId = event.parentHandleId;
    const { session } = d();
    const step = session.steps[session.steps.length - 1];
    if (!step || !handleId) return;

    if (parentHandleId) {
      patchLastStep({
        subagents: routeSubagentWrapper(
          step.subagents ?? [],
          subagentName,
          handleId,
          parentHandleId,
          wrapper,
        ),
      });
      return;
    }

    let tools = step.tools;
    const existingIndex = tools.findIndex(
      (t: ToolResult) =>
        t.handleId === handleId ||
        (t.handleId == null && t.name === event.name),
    );
    if (existingIndex >= 0) {
      tools = tools.map((t: ToolResult, i: number) =>
        i === existingIndex
          ? {
              ...t,
              ...wrapper,
              id: t.id,
              status: wrapper.status,
              summary: wrapper.summary ?? t.summary,
            }
          : t,
      );
    } else {
      tools = [...tools, wrapper];
    }
    patchLastStep({
      tools,
      subagents: routeSubagentWrapper(
        step.subagents ?? [],
        subagentName,
        handleId,
        null,
        wrapper,
      ),
    });
  }

  function findMatchingToolIndex(
    event: AgentEvent,
  ): { stepIndex: number; toolIndex: number } | null {
    const { session } = d();
    for (const preferScoped of [true, false]) {
      for (let i = session.steps.length - 1; i >= 0; i--) {
        const step = session.steps[i];
        for (let j = step.tools.length - 1; j >= 0; j--) {
          const tool = step.tools[j];
          if (tool.name !== event.name) continue;
          if (tool.status !== "running" && tool.summary) continue;
          if (preferScoped) {
            if (
              (event.callScope == null || tool.callScope === event.callScope) &&
              (event.callKind == null || tool.callKind === event.callKind)
            ) {
              return { stepIndex: i, toolIndex: j };
            }
          } else {
            return { stepIndex: i, toolIndex: j };
          }
        }
      }
    }
    return null;
  }

  function handleRegularToolResult(
    event: AgentEvent,
    rawDetail: ToolDetail | string | undefined,
  ): void {
    const { state: s } = d();
    const match = findMatchingToolIndex(event);
    if (!match) return;
    const { session } = d();
    const step = session.steps[match.stepIndex];
    const tool = step.tools[match.toolIndex];
    const status = asToolStatus(event.status, tool.status);
    const updatedTool: ToolResult = applyToolDetail(
      {
        ...tool,
        status,
        summary: event.summary ?? tool.summary,
        skill: event.skill ?? tool.skill,
        skillDescription: event.skillDescription ?? tool.skillDescription ?? "",
        segmentIndex: s.segmentIndex,
        callKind: event.callKind ?? tool.callKind,
        callScope: event.callScope ?? tool.callScope,
        subagentName: event.subagentName ?? tool.subagentName,
        handleId: event.handleId ?? tool.handleId,
        parentHandleId: event.parentHandleId ?? tool.parentHandleId,
        duration: event.duration ?? tool.duration,
        progress: ["done", "error"].includes(status)
          ? undefined
          : tool.progress,
      },
      rawDetail,
    );
    replaceStepAt(match.stepIndex, {
      ...step,
      tools: step.tools.map((t: ToolResult, i: number) =>
        i === match.toolIndex ? updatedTool : t,
      ),
    });
  }

  function handleToolResultEvent(event: AgentEvent): void {
    const { state: s } = d();
    s.segmentIndex++;
    s.currentSegmentType = "tool_result";
    if (!event.name) return;

    const rawDetail: ToolDetail | string | undefined =
      event.detail_data ?? event.detail;

    if (event.callKind === "subagent_run") {
      handleSubagentRunToolResult(event, rawDetail);
      return;
    }
    handleRegularToolResult(event, rawDetail);
  }

  function handleSubagentEndEvent(event: AgentEvent): void {
    const { state: s } = d();
    if (!event.name || !event.handleId) return;
    const handleId = event.handleId;
    const result = isRecord(event.result) ? event.result : undefined;
    const isError = result?.status === "error";
    const errorMessage =
      typeof result?.error === "string" ? result.error : undefined;
    upsertSubagentRun(
      event.name,
      handleId,
      {
        status: isError ? "error" : "completed",
        ...(errorMessage ? { error: errorMessage } : {}),
      },
      event.parentHandleId,
    );

    if (isError && errorMessage) {
      const { session } = d();
      const step = session.steps[session.steps.length - 1];
      if (step) {
        const wrapper = normalizeToolResult({
          id: `tool-${++s.toolIdCounter}`,
          name: event.name,
          skill: "",
          status: "error",
          callKind: "subagent_run",
          callScope: event.parentHandleId ? "subagent" : "parent",
          subagentName: event.name,
          handleId,
          parentHandleId: event.parentHandleId ?? null,
          summary: errorMessage,
        });
        if (event.parentHandleId) {
          patchLastStep({
            subagents: routeSubagentWrapper(
              step.subagents ?? [],
              event.name,
              handleId,
              event.parentHandleId,
              wrapper,
            ),
          });
        } else {
          const existingIndex = step.tools.findIndex(
            (t: ToolResult) => t.name === event.name,
          );
          const tools =
            existingIndex >= 0
              ? step.tools.map((t: ToolResult, i: number) =>
                  i === existingIndex
                    ? { ...t, status: "error" as const, summary: errorMessage }
                    : t,
                )
              : [...step.tools, wrapper];
          patchLastStep({
            tools,
            subagents: routeSubagentWrapper(
              step.subagents ?? [],
              event.name,
              handleId,
              null,
              wrapper,
            ),
          });
        }
      }
    }
  }

  function handleThinkEvent(event: AgentEvent): void {
    if (!event.detail) return;
    const { state: s, session } = d();

    if (event.detail.startsWith("tool_calls:")) {
      const namesPart = event.detail.slice("tool_calls:".length);
      const toolNames = namesPart
        .split(",")
        .map((n) => n.trim())
        .filter(Boolean);
      const lastStep = session.steps[session.steps.length - 1];

      if (
        lastStep &&
        lastStep.tools.length === 0 &&
        lastStep.turnIndex === s.currentTurnIndex
      ) {
        const tools: ToolResult[] = toolNames.map((name) =>
          normalizeToolResult({
            id: `tool-${++s.toolIdCounter}`,
            name,
            skill: "",
            status: "pending",
          }),
        );
        replaceStepAt(session.steps.length - 1, {
          ...lastStep,
          label: toolNames.join(", "),
          tools,
        });
        s.currentStepIndex = lastStep.index;
      } else {
        if (lastStep) patchLastStep({ endSegmentIndex: s.segmentIndex });
        const step = createStep(session.steps.length + 1, toolNames);
        addStep(step);
        s.currentStepIndex = step.index;
        s.observedSinceLastStep = false;
        assignPendingThoughtsToCurrentStep();
        flushPendingSubagents();
      }
      s.observedSinceLastStep = false;
      return;
    }

    if (event.detail === "text_response") {
      s.currentThoughtTurn++;
      const lastStep = session.steps[session.steps.length - 1];
      if (
        !lastStep ||
        lastStep.tools.length > 0 ||
        lastStep.turnIndex !== s.currentTurnIndex
      ) {
        const placeholderStep = createStep(session.steps.length + 1, []);
        addStep(placeholderStep);
        s.currentStepIndex = placeholderStep.index;
        s.observedSinceLastStep = false;
        assignPendingThoughtsToCurrentStep();
      }
    }
  }

  function handleObserveEvent(): void {
    const { state: s, session } = d();
    s.segmentIndex++;
    s.currentSegmentType = "observe";
    s.currentStepIndex = 0;
    s.observedSinceLastStep = true;
    s.currentThoughtTurn++;
    const currentStep = session.steps[session.steps.length - 1];
    if (!currentStep) return;
    const updatedTools = currentStep.tools.map((tool) => {
      if (tool.status === "running" && !tool.summary) {
        return {
          ...tool,
          status: "done" as const,
          segmentIndex: s.segmentIndex,
          duration: tool.startTime
            ? (Date.now() - tool.startTime) / 1000
            : tool.duration,
        };
      }
      return tool;
    });
    patchLastStep({ tools: updatedTools });
  }

  function handleSubagentToolResultEvent(event: AgentEvent): void {
    if (!event.name || !event.handleId || !event.toolName) return;
    const { state: s, session } = d();
    const step = session.steps[session.steps.length - 1];
    if (!step) return;

    const subagentName = event.name;
    const handleId = event.handleId;
    const parentHandleId = event.parentHandleId;
    const toolStatus = asToolStatus(event.toolStatus, "done");
    const tool = normalizeToolResult({
      id: `tool-${++s.toolIdCounter}`,
      name: event.toolName,
      skill: "",
      status: toolStatus,
      callKind: "tool",
      callScope: "subagent",
      subagentName,
      handleId,
      parentHandleId,
      summary: event.toolSummary ?? "",
      duration: event.toolDuration,
    });

    const roots = step.subagents ?? [];
    if (parentHandleId) {
      const parent = findRunningParentByHandleId(roots, parentHandleId);
      if (parent) {
        patchLastStep({
          subagents: updateNodeInTree(roots, parent.handleId, (p) => ({
            ...p,
            children: addSubagentToolInContainer(
              p.children ?? [],
              handleId,
              tool,
            ),
          })),
        });
      }
    } else {
      patchLastStep({
        subagents: addSubagentToolInContainer(roots, handleId, tool),
      });
    }
    newSubagentThoughtBlock(handleId, parentHandleId);
  }

  function handleCompleteEvent(event: AgentEvent): void {
    const { state: s, session, turnVersion } = d();
    session.status = "completed";
    closeLastStepSegmentRange();
    const cTin = event.tokens_in ?? event.tokensIn;
    const cTout = event.tokens_out ?? event.tokensOut;
    if (cTin != null) session.stats.tokensIn = cTin;
    if (cTout != null) session.stats.tokensOut = cTout;
    if (event.conclusion) {
      session.conclusion = event.conclusion;
      if (s.currentTurn) {
        // Prefer the streaming-built conclusion if already accumulated
        // (conclusion_token events), falling back to the complete-event value.
        if (!s.currentTurn.conclusion) {
          s.currentTurn.conclusion = event.conclusion;
        }
        turnVersion.value++;
      }
    }
  }

  // ---- main dispatcher ----

  function handleSessionEvent(event: AgentEvent): void {
    const { state: s, session, turnVersion } = d();

    switch (event.type) {
      case "session": {
        if (event.sessionId) session.id = event.sessionId;
        if (event.modelName) session.modelName = event.modelName;
        break;
      }

      case "think": {
        handleThinkEvent(event);
        break;
      }

      case "act": {
        if (event.detail?.startsWith("executing:")) {
          const namesPart = event.detail.slice("executing:".length);
          const toolNames = namesPart
            .split(",")
            .map((n) => n.trim())
            .filter(Boolean);
          const currentStep = session.steps[session.steps.length - 1];
          if (currentStep) {
            const updatedTools = currentStep.tools.map((tool) => {
              if (toolNames.includes(tool.name) && tool.status === "pending") {
                return {
                  ...tool,
                  status: "running" as const,
                  startTime: Date.now(),
                };
              }
              return tool;
            });
            patchLastStep({ tools: updatedTools });
          }
        }
        break;
      }

      case "observe": {
        handleObserveEvent();
        break;
      }

      case "token": {
        if (event.text) appendThought(event.text);
        break;
      }

      case "tool_start": {
        if (!event.name) break;
        const currentStep = session.steps[session.steps.length - 1];
        if (!currentStep) break;
        const idx = currentStep.tools.findIndex(
          (t) => t.name === event.name && t.status === "pending",
        );
        if (idx >= 0) {
          patchLastStep({
            tools: currentStep.tools.map((t, i) =>
              i === idx
                ? { ...t, status: "running" as const, startTime: Date.now() }
                : t,
            ),
          });
        }
        break;
      }

      case "tool_progress": {
        if (!event.name || !event.progress) break;
        const currentStep = session.steps[session.steps.length - 1];
        if (!currentStep) break;
        const idx = currentStep.tools.findIndex(
          (t) => t.name === event.name && t.status === "running",
        );
        if (idx >= 0) {
          const tool = currentStep.tools[idx];
          const updatedTool: ToolResult = {
            ...tool,
            progress: event.progress.message,
          };
          if (event.progress.detail && isToolDetail(event.progress.detail)) {
            updatedTool.detail = event.progress.detail;
          }
          patchLastStep({
            tools: currentStep.tools.map((t, i) =>
              i === idx ? updatedTool : t,
            ),
          });
        }
        break;
      }

      case "tool_result": {
        handleToolResultEvent(event);
        break;
      }

      case "subagent_start": {
        if (!event.name || !event.handleId) break;
        s.segmentIndex++;
        s.currentSegmentType = "tool_result";
        s.currentThoughtTurn++;
        const step = session.steps[session.steps.length - 1];
        if (step) {
          upsertSubagentRun(
            event.name,
            event.handleId,
            { task: event.task, status: "running" },
            event.parentHandleId,
          );
        } else {
          s.pendingSubagents.push({
            run: {
              name: event.name,
              handleId: event.handleId,
              task: event.task ?? "",
              status: "running",
            },
            parentHandleId: event.parentHandleId,
          });
        }
        break;
      }

      case "subagent_token": {
        if (!event.name || !event.handleId || typeof event.text !== "string")
          break;
        appendSubagentThought(event.handleId, event.text, event.parentHandleId);
        break;
      }

      case "subagent_think": {
        if (!event.name || !event.handleId || typeof event.text !== "string")
          break;
        if (event.text === "text_response") {
          newSubagentThoughtBlock(event.handleId, event.parentHandleId);
        } else if (event.text) {
          appendSubagentThought(
            event.handleId,
            event.text,
            event.parentHandleId,
          );
        }
        break;
      }

      case "subagent_conclusion": {
        if (!event.name || !event.handleId) break;
        upsertSubagentRun(
          event.name,
          event.handleId,
          { conclusion: event.text },
          event.parentHandleId,
        );
        break;
      }

      case "subagent_tool_result": {
        handleSubagentToolResultEvent(event);
        break;
      }

      case "subagent_end": {
        handleSubagentEndEvent(event);
        break;
      }

      case "usage": {
        const tin = event.tokens_in ?? event.tokensIn;
        const tout = event.tokens_out ?? event.tokensOut;
        if (tin != null) session.stats.tokensIn += tin;
        if (tout != null) session.stats.tokensOut += tout;
        break;
      }

      case "conclusion_token": {
        if (!event.text) break;
        if (s.currentTurn) {
          s.currentTurn.conclusion =
            (s.currentTurn.conclusion ?? "") + event.text;
          turnVersion.value++;
        }
        break;
      }

      case "complete": {
        handleCompleteEvent(event);
        break;
      }

      case "stopped": {
        session.status = "completed";
        closeLastStepSegmentRange();
        session.stopReason = "user";
        finalizeRunningOperations(session, "cancelled", "error", "用户已停止");
        break;
      }

      case "error": {
        session.status = "error";
        closeLastStepSegmentRange();
        if (event.detail) session.errorMessage = event.detail;
        else if (event.text) session.errorMessage = event.text;
        finalizeRunningOperations(session, "error", "error", "异常结束");
        break;
      }
    }
  }

  return { handleSessionEvent };
}
