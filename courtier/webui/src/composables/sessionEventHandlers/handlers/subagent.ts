import type { AgentEvent } from "../../../types/agent";
import {
  appendSubagentThought,
  newSubagentThoughtBlock,
  upsertSubagentRun,
} from "../state";
import {
  handleSubagentEndEvent,
  handleSubagentToolResultEvent,
} from "../subagent";
import type { HandlerDeps } from "../types";

export function handleSubagentEvent(
  deps: () => HandlerDeps,
  event: AgentEvent,
): void {
  const { state: s, session } = deps();

  switch (event.type) {
    case "subagent_start": {
      if (!event.name || !event.handleId) break;
      s.segmentIndex++;
      s.currentSegmentType = "tool_result";
      s.currentThoughtTurn++;
      const step = session.steps[session.steps.length - 1];
      if (step) {
        upsertSubagentRun(
          deps,
          event.name,
          event.handleId,
          {
            task: event.task,
            status: "running",
            displayName: event.displayName ?? null,
          },
          event.parentHandleId,
        );
      } else {
        s.pendingSubagents.push({
          run: {
            name: event.name,
            handleId: event.handleId,
            task: event.task ?? "",
            status: "running",
            displayName: event.displayName ?? null,
          },
          parentHandleId: event.parentHandleId,
        });
      }
      break;
    }

    case "subagent_token": {
      if (!event.name || !event.handleId || typeof event.text !== "string")
        break;
      appendSubagentThought(
        deps,
        event.handleId,
        event.text,
        event.parentHandleId,
      );
      break;
    }

    case "subagent_think": {
      if (!event.name || !event.handleId || typeof event.text !== "string")
        break;
      if (event.text === "text_response") {
        newSubagentThoughtBlock(
          deps,
          event.handleId,
          event.parentHandleId,
        );
      } else if (event.text) {
        appendSubagentThought(
          deps,
          event.handleId,
          event.text,
          event.parentHandleId,
        );
      }
      break;
    }

    case "subagent_conclusion": {
      if (!event.name || !event.handleId) break;
      upsertSubagentRun(
        deps,
        event.name,
        event.handleId,
        { conclusion: event.text },
        event.parentHandleId,
      );
      break;
    }

    case "subagent_tool_result": {
      handleSubagentToolResultEvent(deps, event);
      break;
    }

    case "subagent_end": {
      handleSubagentEndEvent(deps, event);
      break;
    }
  }
}
