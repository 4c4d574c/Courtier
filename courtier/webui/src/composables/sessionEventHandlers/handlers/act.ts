import type { AgentEvent } from "../../../types/agent";
import { patchLastStep } from "../state";
import type { HandlerDeps } from "../types";

export function handleActEvent(
  deps: () => HandlerDeps,
  event: AgentEvent,
): void {
  const { session } = deps();

  // Prefer the structured tools list; fall back to the legacy detail string.
  const toolNames =
    event.tools ??
    (event.detail?.startsWith("executing:")
      ? event.detail
          .slice("executing:".length)
          .split(",")
          .map((n) => n.trim())
          .filter(Boolean)
      : undefined);
  if (!toolNames) return;

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
    patchLastStep(deps, { tools: updatedTools });
  }
}
