import type { AgentEvent } from "../../../types/agent";
import { finalizeRunningOperations } from "../finalize";
import { closeLastStepSegmentRange } from "../state";
import { handleCompleteEvent } from "../complete";
import type { HandlerDeps } from "../types";

export function handleCompletionEvent(
  deps: () => HandlerDeps,
  event: AgentEvent,
): void {
  const { session, state: s } = deps();

  switch (event.type) {
    case "complete": {
      handleCompleteEvent(deps, event);
      break;
    }
    case "stopped": {
      session.status = "completed";
      closeLastStepSegmentRange(deps);
      session.stopReason = "user";
      session.compacting = false;
      finalizeRunningOperations(session, "cancelled", "error", "用户已停止");
      // 中断时缓冲中未定性文本落到结论，避免流式内容直接消失。
      if (session.pendingVerdict && s.currentTurn) {
        s.currentTurn.conclusion = session.pendingVerdict;
      }
      session.pendingVerdict = "";
      session.pendingVerdictAfterStepIndex = 0;
      break;
    }
    case "error": {
      session.status = "error";
      closeLastStepSegmentRange(deps);
      if (event.detail) session.errorMessage = event.detail;
      else if (event.text) session.errorMessage = event.text;
      s.pendingSubagents = [];
      session.compacting = false;
      finalizeRunningOperations(session, "error", "error", "异常结束");
      if (session.pendingVerdict && s.currentTurn) {
        s.currentTurn.conclusion = session.pendingVerdict;
      }
      session.pendingVerdict = "";
      session.pendingVerdictAfterStepIndex = 0;
      break;
    }
  }
}
