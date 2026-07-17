import type { AgentEvent, ToolResult } from "../../../types/agent";
import { isToolDetail } from "../utils";
import { patchLastStep } from "../state";
import type { HandlerDeps } from "../types";

export function handleToolStartEvent(
  deps: () => HandlerDeps,
  event: AgentEvent,
): void {
  if (!event.name) return;
  const { session } = deps();
  const currentStep = session.steps[session.steps.length - 1];
  if (!currentStep) return;
  const idx = currentStep.tools.findIndex(
    (t) => t.name === event.name && t.status === "pending",
  );
  if (idx >= 0) {
    patchLastStep(deps, {
      tools: currentStep.tools.map((t, i) =>
        i === idx
          ? { ...t, status: "running" as const, startTime: Date.now() }
          : t,
      ),
    });
  }
}

export function handleToolProgressEvent(
  deps: () => HandlerDeps,
  event: AgentEvent,
): void {
  if (!event.name || !event.progress) return;
  const { session } = deps();
  const currentStep = session.steps[session.steps.length - 1];
  if (!currentStep) return;
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
    patchLastStep(deps, {
      tools: currentStep.tools.map((t, i) =>
        i === idx ? updatedTool : t,
      ),
    });
  }
}
