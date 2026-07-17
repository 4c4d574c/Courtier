import type { AgentEvent } from "../../../types/agent";
import type { HandlerDeps } from "../types";

export function handleSessionEvent(
  deps: () => HandlerDeps,
  event: AgentEvent,
): void {
  const { session } = deps();
  if (event.sessionId) session.id = event.sessionId;
  if (event.modelName) session.modelName = event.modelName;
}
