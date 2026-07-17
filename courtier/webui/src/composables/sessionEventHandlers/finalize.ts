import type {
  Session,
  Step,
  SubagentRun,
  ToolResult,
  ToolStatus,
} from "../../types/agent";

// ---------------------------------------------------------------------------
// Finalize running/pending operations when the session ends abnormally
// ---------------------------------------------------------------------------

export function finalizeRunningOperations(
  session: Session,
  toolStatus: ToolStatus = "error",
  runStatus: SubagentRun["status"] = "error",
  summary?: string,
): void {
  const now = Date.now();

  function finalizeTool(tool: ToolResult): ToolResult {
    if (tool.status !== "running" && tool.status !== "pending") return tool;
    const duration =
      tool.status === "running" && tool.startTime
        ? (now - tool.startTime) / 1000
        : tool.duration;
    return {
      ...tool,
      status: toolStatus,
      duration,
      summary: summary ?? tool.summary,
    };
  }

  function finalizeRun(run: SubagentRun): SubagentRun {
    const statusChanged = run.status === "running";
    return {
      ...run,
      status: statusChanged ? runStatus : run.status,
      error: statusChanged && summary ? summary : run.error,
      tools: run.tools?.map(finalizeTool),
      wrapper: run.wrapper ? finalizeTool(run.wrapper) : undefined,
      children: run.children?.map(finalizeRun),
    };
  }

  for (let i = 0; i < session.steps.length; i++) {
    const step = session.steps[i];
    const tools = step.tools.map(finalizeTool);
    const subagents = step.subagents?.map(finalizeRun);
    const newStep: Step = { ...step, tools, subagents };
    session.steps[i] = newStep;
    if (step.turnIndex !== undefined) {
      const turn = session.turns[step.turnIndex - 1];
      if (turn) {
        const idx = turn.steps.findIndex((st) => st.index === step.index);
        if (idx >= 0) {
          turn.steps[idx] = newStep;
        }
      }
    }
  }
}
