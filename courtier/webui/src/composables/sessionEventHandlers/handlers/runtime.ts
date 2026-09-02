import type { AgentEvent } from "../../../types/agent";
import { pushRuntimeEvent } from "../utils";
import type { HandlerDeps } from "../types";

export function handleRuntimeEvent(
  deps: () => HandlerDeps,
  event: AgentEvent,
): void {
  const { session } = deps();

  switch (event.type) {
    case "guard_triggered": {
      pushRuntimeEvent(session, "guardEvents", event);
      break;
    }
    case "hint_injected": {
      pushRuntimeEvent(session, "hintEvents", event);
      break;
    }
    case "model_selected": {
      pushRuntimeEvent(session, "modelEvents", event);
      // 每次运行广播实际所用模型 — 输入区展示随切换实时更新。
      if (event.model) session.modelName = event.model;
      session.lastModelId = event.modelId || "";
      break;
    }
    case "model_fallback": {
      pushRuntimeEvent(session, "modelEvents", event);
      break;
    }
    case "loop_completed": {
      session.loopCompleted = {
        type: "loop_completed",
        status: event.status,
        terminationReason: event.terminationReason,
        totalSteps: event.totalSteps,
      };
      break;
    }
  }
}
