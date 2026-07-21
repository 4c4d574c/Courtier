import type { AgentEvent, ToolResult } from "../../../types/agent";
import { normalizeToolResult } from "../../../utils/toolCalls";
import {
  addStep,
  assignPendingThoughtsToCurrentStep,
  createStep,
  flushPendingSubagents,
  patchLastStep,
  replaceStepAt,
} from "../state";
import type { HandlerDeps } from "../types";

export function handleThinkEvent(
  deps: () => HandlerDeps,
  event: AgentEvent,
): void {
  const { state: s, session } = deps();

  // Prefer the structured payload; fall back to the legacy detail string.
  const toolNames =
    event.toolCalls ??
    (event.detail?.startsWith("tool_calls:")
      ? event.detail
          .slice("tool_calls:".length)
          .split(",")
          .map((n) => n.trim())
          .filter(Boolean)
      : undefined);

  if (toolNames) {
    const displayNames = event.displayNames ?? {};
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
          displayName: displayNames[name] ?? null,
          skill: "",
          status: "pending",
        }),
      );
      replaceStepAt(deps, session.steps.length - 1, {
        ...lastStep,
        label: toolNames.join(", "),
        tools,
      });
      s.currentStepIndex = lastStep.index;
    } else {
      if (lastStep) patchLastStep(deps, { endSegmentIndex: s.segmentIndex });
      const step = createStep(deps, session.steps.length + 1, toolNames, displayNames);
      addStep(deps, step);
      s.currentStepIndex = step.index;
      assignPendingThoughtsToCurrentStep(deps);
      flushPendingSubagents(deps);
    }
    s.observedSinceLastStep = false;
    return;
  }

  if (event.textResponse || event.detail === "text_response") {
    s.currentThoughtTurn++;
    const lastStep = session.steps[session.steps.length - 1];
    if (
      !lastStep ||
      lastStep.tools.length > 0 ||
      lastStep.turnIndex !== s.currentTurnIndex
    ) {
      const placeholderStep = createStep(deps, session.steps.length + 1, []);
      addStep(deps, placeholderStep);
      s.currentStepIndex = placeholderStep.index;
      s.observedSinceLastStep = false;
      assignPendingThoughtsToCurrentStep(deps);
    }
  }
}
