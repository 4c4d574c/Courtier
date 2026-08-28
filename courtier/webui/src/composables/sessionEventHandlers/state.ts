import type { Step, SubagentRun, ToolResult } from "../../types/agent";
import { normalizeToolResult } from "../../utils/toolCalls";
import { toChineseNumeral } from "../../utils/chineseNumerals";
import {
  appendSubagentThoughtInContainer,
  findRunningParentByHandleId,
  newSubagentThoughtBlockInContainer,
  updateNodeInTree,
  upsertNodeInTree,
} from "../subagentTree";
import type { HandlerDeps } from "./types";

// ---- step lifecycle helpers ----

export function createStep(
  deps: () => HandlerDeps,
  index: number,
  toolNames: string[],
  displayNames: Record<string, string | null> = {},
  toolCallIds: string[] = [],
): Step {
  const { state: s } = deps();
  const tools: ToolResult[] = toolNames.map((name, i) =>
    normalizeToolResult({
      id: `tool-${++s.toolIdCounter}`,
      name,
      displayName: displayNames[name] ?? null,
      skill: "",
      status: "pending",
      // Same-name parallel calls are paired to results by this id.
      toolCallId: toolCallIds[i] ?? null,
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

export function replaceStepAt(
  deps: () => HandlerDeps,
  stepIndex: number,
  updatedStep: Step,
): void {
  const { session } = deps();
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

export function patchLastStep(
  deps: () => HandlerDeps,
  patch: Partial<Step>,
): void {
  const { session } = deps();
  const lastIndex = session.steps.length - 1;
  const step = session.steps[lastIndex];
  if (!step) return;
  replaceStepAt(deps, lastIndex, { ...step, ...patch });
}

export function addStep(deps: () => HandlerDeps, step: Step): boolean {
  const { state: s, session } = deps();
  session.steps.push(step);
  if (s.currentTurn) {
    s.currentTurn.steps.push(step);
    return true;
  }
  return false;
}

// ---- thought helpers ----

export function appendThought(
  deps: () => HandlerDeps,
  text: string,
  source?: string,
): void {
  const { state: s, session } = deps();
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

export function assignPendingThoughtsToCurrentStep(
  deps: () => HandlerDeps,
): void {
  const { state: s, session } = deps();
  if (s.currentStepIndex <= 0) return;
  session.thoughts = session.thoughts.map((t) => {
    if (t.source || t.stepIndex !== undefined) return t;
    return { ...t, stepIndex: s.currentStepIndex };
  });
}

// ---- sub-agent helpers ----

export function nextSubagentThoughtId(
  deps: () => HandlerDeps,
  handleId: string,
): number {
  const { state: s } = deps();
  const next = (s.subagentThoughtCounters[handleId] ?? 0) + 1;
  s.subagentThoughtCounters[handleId] = next;
  return next;
}

export function flushPendingSubagents(deps: () => HandlerDeps): void {
  const { state: s, session } = deps();
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
  patchLastStep(deps, { subagents: roots });
  s.pendingSubagents = [];
}

export function upsertSubagentRun(
  deps: () => HandlerDeps,
  name: string,
  handleId: string,
  patch: Partial<SubagentRun>,
  parentHandleId?: string | null,
): void {
  const { state: s, session } = deps();
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
  patchLastStep(deps, {
    subagents: upsertNodeInTree(
      step.subagents ?? [],
      name,
      handleId,
      parentHandleId,
      patch,
    ),
  });
}

export function appendSubagentThought(
  deps: () => HandlerDeps,
  handleId: string,
  text: string,
  parentHandleId?: string | null,
): void {
  const { session } = deps();
  const step = session.steps[session.steps.length - 1];
  if (!step) return;
  const roots = step.subagents ?? [];
  const nextId = nextSubagentThoughtId(deps, handleId);

  if (parentHandleId) {
    const parent = findRunningParentByHandleId(roots, parentHandleId);
    if (parent) {
      patchLastStep(deps, {
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
  patchLastStep(deps, {
    subagents: appendSubagentThoughtInContainer(
      roots,
      handleId,
      text,
      nextId,
    ),
  });
}

export function newSubagentThoughtBlock(
  deps: () => HandlerDeps,
  handleId: string,
  parentHandleId?: string | null,
): void {
  const { session } = deps();
  const step = session.steps[session.steps.length - 1];
  if (!step) return;
  const nextId = nextSubagentThoughtId(deps, handleId);
  const roots = step.subagents ?? [];

  if (parentHandleId) {
    const parent = findRunningParentByHandleId(roots, parentHandleId);
    if (parent) {
      patchLastStep(deps, {
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
  patchLastStep(deps, {
    subagents: newSubagentThoughtBlockInContainer(roots, handleId, nextId),
  });
}

export function closeLastStepSegmentRange(deps: () => HandlerDeps): void {
  const { session } = deps();
  const lastStep = session.steps[session.steps.length - 1];
  if (!lastStep || lastStep.endSegmentIndex != null) return;
  patchLastStep(deps, { endSegmentIndex: deps().state.segmentIndex });
}
