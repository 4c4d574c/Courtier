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
import {
  handleToolStartEvent,
  handleToolProgressEvent,
} from "./handlers/toolLifecycle";
import { handleToolResultEventWrapper } from "./handlers/toolResult";
import { handleSubagentEvent } from "./handlers/subagent";
import { handleUsageEvent } from "./handlers/usage";
import { handleCompletionEvent } from "./handlers/completion";
import { handleRuntimeEvent } from "./handlers/runtime";

export { finalizeRunningOperations };
export type { MutableState, HandlerDeps, DepsFn };

export function createSessionEventHandlers(deps: () => HandlerDeps) {
  function handleSessionEvent(event: AgentEvent): void {
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
      case "complete":
      case "stopped":
      case "error": {
        handleCompletionEvent(deps, event);
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
