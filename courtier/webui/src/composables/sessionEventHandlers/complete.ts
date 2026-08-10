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
  // 流式缓冲中剩余的就是最终结论（中间过程文本已在 observe 时经
  // step_verdict 事件转正到各 step）；complete 事件的全量结论仅作兜底。
  const streamed = session.pendingVerdict ?? "";
  session.pendingVerdict = "";
  session.pendingVerdictAfterStepIndex = 0;
  session.compacting = false;
  if (event.conclusion) {
    session.conclusion = event.conclusion;
  }
  if (s.currentTurn && (streamed || event.conclusion)) {
    s.currentTurn.conclusion =
      streamed || s.currentTurn.conclusion || event.conclusion;
    turnVersion.value++;
  }
}
