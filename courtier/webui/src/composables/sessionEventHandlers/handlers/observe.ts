import type { AgentEvent } from "../../../types/agent";
import { patchLastStep } from "../state";
import type { HandlerDeps } from "../types";

export function handleObserveEvent(
  deps: () => HandlerDeps,
  _event: AgentEvent,
): void {
  const { state: s, session } = deps();
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
  patchLastStep(deps, { tools: updatedTools });
}
