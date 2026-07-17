import type { AgentEvent } from "../../../types/agent";
import { appendThought } from "../state";
import type { HandlerDeps } from "../types";

export function handleTokenEvent(
  deps: () => HandlerDeps,
  event: AgentEvent,
): void {
  if (event.text) appendThought(deps, event.text);
}

export function handleConclusionTokenEvent(
  deps: () => HandlerDeps,
  event: AgentEvent,
): void {
  const { state: s, turnVersion } = deps();
  if (!event.text) return;
  if (s.currentTurn) {
    s.currentTurn.conclusion =
      (s.currentTurn.conclusion ?? "") + event.text;
    turnVersion.value++;
  }
}
