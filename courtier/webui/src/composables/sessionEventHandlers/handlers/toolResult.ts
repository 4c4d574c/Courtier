import type { AgentEvent } from "../../../types/agent";
import { handleToolResultEvent } from "../tool";
import type { HandlerDeps } from "../types";

export function handleToolResultEventWrapper(
  deps: () => HandlerDeps,
  event: AgentEvent,
): void {
  handleToolResultEvent(deps, event);
}
