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
      finalizeRunningOperations(session, "cancelled", "error", "用户已停止");
      break;
    }
    case "error": {
      session.status = "error";
      closeLastStepSegmentRange(deps);
      if (event.detail) session.errorMessage = event.detail;
      else if (event.text) session.errorMessage = event.text;
      s.pendingSubagents = [];
      finalizeRunningOperations(session, "error", "error", "异常结束");
      break;
    }
  }
}
