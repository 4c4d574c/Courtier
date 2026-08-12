import type { AgentEvent, ToolDetail, ToolResult } from "../../types/agent";
import { normalizeToolResult } from "../../utils/toolCalls";
import { applyToolDetail, asToolStatus, findMatchingToolIndex } from "./utils";
import { patchLastStep, replaceStepAt } from "./state";
import { routeSubagentWrapper } from "../subagentTree";
import type { HandlerDeps } from "./types";

export function handleSubagentRunToolResult(
  deps: () => HandlerDeps,
  event: AgentEvent,
  rawDetail: ToolDetail | string | undefined,
): void {
  const { state: s } = deps();
  const status = asToolStatus(event.status, "done");
  const wrapper: ToolResult = applyToolDetail(
    normalizeToolResult({
      id: `tool-${++s.toolIdCounter}`,
      name: event.name!,
      displayName: event.displayName ?? null,
      skill: event.skill ?? "",
      skillDescription: event.skillDescription ?? "",
      status,
      callKind: event.callKind ?? "subagent_run",
      callScope: event.callScope ?? "parent",
      subagentName: event.subagentName ?? event.name!,
      handleId: event.handleId ?? null,
      parentHandleId: event.parentHandleId ?? null,
      summary: event.summary ?? "",
      duration: event.duration,
      issueCounts: event.issueCounts,
    }),
    rawDetail,
  );

  const subagentName = event.subagentName ?? event.name!;
  const handleId = event.handleId;
  const parentHandleId = event.parentHandleId;
  const { session } = deps();
  const step = session.steps[session.steps.length - 1];
  if (!step || !handleId) return;

  if (parentHandleId) {
    patchLastStep(deps, {
      subagents: routeSubagentWrapper(
        step.subagents ?? [],
        subagentName,
        handleId,
        parentHandleId,
        wrapper,
      ),
    });
    return;
  }

  let tools = step.tools;
  const existingIndex = tools.findIndex(
    (t: ToolResult) =>
      t.handleId === handleId ||
      (t.handleId == null && t.name === event.name),
  );
  if (existingIndex >= 0) {
    tools = tools.map((t: ToolResult, i: number) =>
      i === existingIndex
        ? {
            ...t,
            ...wrapper,
            id: t.id,
            status: wrapper.status,
            summary: wrapper.summary ?? t.summary,
          }
        : t,
    );
  } else {
    tools = [...tools, wrapper];
  }
  patchLastStep(deps, {
    tools,
    subagents: routeSubagentWrapper(
      step.subagents ?? [],
      subagentName,
      handleId,
      null,
      wrapper,
    ),
  });
}

export function handleRegularToolResult(
  deps: () => HandlerDeps,
  event: AgentEvent,
  rawDetail: ToolDetail | string | undefined,
): void {
  const { state: s } = deps();
  const match = findMatchingToolIndex(deps().session, event);
  if (!match) return;
  const { session } = deps();
  const step = session.steps[match.stepIndex];
  const tool = step.tools[match.toolIndex];
  const status = asToolStatus(event.status, tool.status);
  const updatedTool: ToolResult = applyToolDetail(
    {
      ...tool,
      status,
      summary: event.summary ?? tool.summary,
      skill: event.skill ?? tool.skill,
      skillDescription: event.skillDescription ?? tool.skillDescription ?? "",
      displayName: event.displayName ?? tool.displayName,
      segmentIndex: s.segmentIndex,
      callKind: event.callKind ?? tool.callKind,
      callScope: event.callScope ?? tool.callScope,
      subagentName: event.subagentName ?? tool.subagentName,
      handleId: event.handleId ?? tool.handleId,
      parentHandleId: event.parentHandleId ?? tool.parentHandleId,
      duration: event.duration ?? tool.duration,
      issueCounts: event.issueCounts ?? tool.issueCounts,
      citations: event.citations ?? tool.citations,
      progress: ["done", "error"].includes(status)
        ? undefined
        : tool.progress,
    },
    rawDetail,
  );
  replaceStepAt(deps, match.stepIndex, {
    ...step,
    tools: step.tools.map((t: ToolResult, i: number) =>
      i === match.toolIndex ? updatedTool : t,
    ),
  });
}

export function handleToolResultEvent(
  deps: () => HandlerDeps,
  event: AgentEvent,
): void {
  const { state: s } = deps();
  s.segmentIndex++;
  s.currentSegmentType = "tool_result";
  if (!event.name) return;

  const rawDetail: ToolDetail | string | undefined =
    event.detail_data ?? event.detail;

  if (event.callKind === "subagent_run") {
    handleSubagentRunToolResult(deps, event, rawDetail);
    return;
  }
  handleRegularToolResult(deps, event, rawDetail);
}
