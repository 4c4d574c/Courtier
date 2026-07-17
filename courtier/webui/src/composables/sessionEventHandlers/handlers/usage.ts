import type { AgentEvent } from "../../../types/agent";
import type { HandlerDeps } from "../types";

export function handleUsageEvent(
  deps: () => HandlerDeps,
  event: AgentEvent,
): void {
  const { session } = deps();
  const tin = event.tokens_in ?? event.tokensIn;
  const tout = event.tokens_out ?? event.tokensOut;
  if (tin != null) session.stats.tokensIn += tin;
  if (tout != null) session.stats.tokensOut += tout;
}
