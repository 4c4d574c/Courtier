import type { AgentEvent } from "../../types/agent";
import type { DepsFn, HandlerDeps, MutableState } from "./types";

import { finalizeRunningOperations } from "./finalize";

import { handleSessionEvent as handleSession } from "./handlers/session";
import { handleThinkEvent } from "./handlers/think";
import { handleActEvent } from "./handlers/act";
import { handleObserveEvent } from "./handlers/observe";
import {
  handleTokenEvent,
  handleConclusionTokenEvent,
} from "./handlers/token";
import { handleStepVerdictEvent } from "./handlers/stepVerdict";
import {
  handleToolStartEvent,
  handleToolProgressEvent,
} from "./handlers/toolLifecycle";
import { handleToolResultEventWrapper } from "./handlers/toolResult";
import { handleSubagentEvent } from "./handlers/subagent";
import { handleUsageEvent } from "./handlers/usage";
import { handleCompletionEvent } from "./handlers/completion";
import { handleRuntimeEvent } from "./handlers/runtime";
import {
  handleConfirmationRequested,
  handleConfirmationResolved,
} from "./handlers/confirmation";
import {
  handleThinkRetryEvent,
  handleRefusalExhaustedEvent,
} from "./handlers/thinkRetry";

export { finalizeRunningOperations };
export type { MutableState, HandlerDeps, DepsFn };

export function createSessionEventHandlers(deps: () => HandlerDeps) {
  function handleSessionEvent(event: AgentEvent): void {
    // Any live event means the run left the server-side queue.
    const { session } = deps();
    if (event.type !== "queued") session.queuePosition = undefined;
    switch (event.type) {
      case "session": {
        handleSession(deps, event);
        break;
      }
      case "think": {
        handleThinkEvent(deps, event);
        break;
      }
      case "act": {
        handleActEvent(deps, event);
        break;
      }
      case "observe": {
        handleObserveEvent(deps, event);
        break;
      }
      case "token": {
        handleTokenEvent(deps, event);
        break;
      }
      case "tool_start": {
        handleToolStartEvent(deps, event);
        break;
      }
      case "tool_progress": {
        handleToolProgressEvent(deps, event);
        break;
      }
      case "tool_result": {
        handleToolResultEventWrapper(deps, event);
        break;
      }
      case "subagent_start":
      case "subagent_token":
      case "subagent_think":
      case "subagent_conclusion":
      case "subagent_tool_result":
      case "subagent_end": {
        handleSubagentEvent(deps, event);
        break;
      }
      case "usage": {
        handleUsageEvent(deps, event);
        break;
      }
      case "conclusion_token": {
        handleConclusionTokenEvent(deps, event);
        break;
      }
      case "conclusion_reset": {
        // A streamed attempt was dropped (tool_calls fallback): clear the
        // step's streamed conclusion so the replacement renders cleanly.
        session.conclusion = "";
        session.pendingVerdict = "";
        break;
      }
      case "step_verdict": {
        handleStepVerdictEvent(deps, event);
        break;
      }
      case "complete":
      case "stopped":
      case "error": {
        const { session } = deps();
        session.queuePosition = undefined;
        handleCompletionEvent(deps, event);
        break;
      }
      case "think_retry": {
        handleThinkRetryEvent(deps, event);
        break;
      }
      case "refusal_exhausted": {
        handleRefusalExhaustedEvent(deps, event);
        break;
      }
      case "confirmation_requested": {
        handleConfirmationRequested(deps, event);
        break;
      }
      case "confirmation_resolved": {
        handleConfirmationResolved(deps, event);
        break;
      }
      case "queued": {
        const { session } = deps();
        session.queuePosition = event.position ?? 0;
        break;
      }
      case "resync":
        // Handled at the connection level (useAgentSession): the watermark
        // was evicted from the bounded log → snapshot reload + re-attach.
        break;
      case "context_compacting": {
        const { session } = deps();
        session.compacting = true;
        break;
      }
      case "context_compacted": {
        const { state: s, session } = deps();
        session.compacting = false;
        session.compactions = [
          ...(session.compactions ?? []),
          {
            text: event.detail || "上下文已压缩",
            turnIndex: s.currentTurnIndex,
            timestamp: Date.now(),
          },
        ];
        break;
      }
      case "guard_triggered":
      case "hint_injected":
      case "model_selected":
      case "model_fallback":
      case "loop_completed": {
        handleRuntimeEvent(deps, event);
        break;
      }
    }
  }

  return { handleSessionEvent };
}
