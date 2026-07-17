import type { AgentEvent } from "../../types/agent";
import { closeLastStepSegmentRange } from "./state";
import type { HandlerDeps } from "./types";

export function handleCompleteEvent(
  deps: () => HandlerDeps,
  event: AgentEvent,
): void {
  const { state: s, session, turnVersion } = deps();
  session.status = "completed";
  closeLastStepSegmentRange(deps);
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
