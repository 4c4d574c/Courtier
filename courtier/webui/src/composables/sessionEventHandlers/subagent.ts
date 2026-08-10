import type { AgentEvent, ToolResult } from "../../types/agent";
import { normalizeToolResult } from "../../utils/toolCalls";
import { asToolStatus, isRecord } from "./utils";
import {
  newSubagentThoughtBlock,
  patchLastStep,
  upsertSubagentRun,
} from "./state";
import {
  addSubagentToolInContainer,
  findRunningParentByHandleId,
  routeSubagentWrapper,
  updateNodeInTree,
} from "../subagentTree";
import type { HandlerDeps } from "./types";

export function handleSubagentEndEvent(
  deps: () => HandlerDeps,
  event: AgentEvent,
): void {
  const { state: s } = deps();
  if (!event.name || !event.handleId) return;
  const handleId = event.handleId;
  const result = isRecord(event.result) ? event.result : undefined;
  const isError = result?.status === "error";
  const errorMessage =
    typeof result?.error === "string" ? result.error : undefined;
  upsertSubagentRun(
    deps,
    event.name,
    handleId,
    {
      status: isError ? "error" : "completed",
      ...(errorMessage ? { error: errorMessage } : {}),
    },
    event.parentHandleId,
  );

  if (isError && errorMessage) {
    const { session } = deps();
    const step = session.steps[session.steps.length - 1];
    if (step) {
      const wrapper = normalizeToolResult({
        id: `tool-${++s.toolIdCounter}`,
        name: event.name,
        skill: "",
        status: "error",
        callKind: "subagent_run",
        callScope: event.parentHandleId ? "subagent" : "parent",
        subagentName: event.name,
        handleId,
        parentHandleId: event.parentHandleId ?? null,
        summary: errorMessage,
      });
      if (event.parentHandleId) {
        patchLastStep(deps, {
          subagents: routeSubagentWrapper(
            step.subagents ?? [],
            event.name,
            handleId,
            event.parentHandleId,
            wrapper,
          ),
        });
      } else {
        const existingIndex = step.tools.findIndex(
          (t: ToolResult) => t.name === event.name,
        );
        const tools =
          existingIndex >= 0
            ? step.tools.map((t: ToolResult, i: number) =>
                i === existingIndex
                  ? { ...t, status: "error" as const, summary: errorMessage }
                  : t,
              )
            : [...step.tools, wrapper];
        patchLastStep(deps, {
          tools,
          subagents: routeSubagentWrapper(
            step.subagents ?? [],
            event.name,
            handleId,
            null,
            wrapper,
          ),
        });
      }
    }
  }
}

export function handleSubagentToolResultEvent(
  deps: () => HandlerDeps,
  event: AgentEvent,
): void {
  if (!event.name || !event.handleId || !event.toolName) return;
  const { state: s, session } = deps();
  const step = session.steps[session.steps.length - 1];
  if (!step) return;

  const subagentName = event.name;
  const handleId = event.handleId;
  const parentHandleId = event.parentHandleId;
  const toolStatus = asToolStatus(event.toolStatus, "done");
  const tool = normalizeToolResult({
    id: `tool-${++s.toolIdCounter}`,
    name: event.toolName,
    displayName: event.displayName ?? null,
    skill: "",
    status: toolStatus,
    callKind: "tool",
    callScope: "subagent",
    subagentName,
    handleId,
    parentHandleId,
    summary: event.toolSummary ?? "",
    duration: event.toolDuration,
    issueCounts: event.issueCounts,
  });

  const roots = step.subagents ?? [];
  const parent = parentHandleId
    ? findRunningParentByHandleId(roots, parentHandleId)
    : undefined;
  if (parent) {
    patchLastStep(deps, {
      subagents: updateNodeInTree(roots, parent.handleId, (p) => ({
        ...p,
        children: addSubagentToolInContainer(
          p.children ?? [],
          handleId,
          tool,
        ),
      })),
    });
  } else {
    // Top-level attach.  Also covers parentHandleId pointing at the
    // orchestrator root handle, which is not a sub-agent node in the tree —
    // mirroring appendSubagentThought's fallback so tools are not dropped.
    patchLastStep(deps, {
      subagents: addSubagentToolInContainer(roots, handleId, tool),
    });
  }
  newSubagentThoughtBlock(deps, handleId, parentHandleId);
}
