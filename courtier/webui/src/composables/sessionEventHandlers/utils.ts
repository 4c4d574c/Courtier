import type {
  AgentEvent,
  RuntimeEvent,
  Session,
  ToolDetail,
  ToolResult,
  ToolStatus,
} from "../../types/agent";

const VALID_TOOL_STATUSES: readonly string[] = [
  "pending",
  "running",
  "done",
  "error",
  "warning",
  "cancelled",
];

export function asToolStatus(
  s: string | undefined,
  fallback: ToolStatus = "done",
): ToolStatus {
  if (s === "ok") return "done";
  if (s && VALID_TOOL_STATUSES.includes(s)) return s as ToolStatus;
  return fallback;
}

export function isToolDetail(v: unknown): v is ToolDetail {
  if (!v || typeof v !== "object") return false;
  const d = v as Record<string, unknown>;
  return d.type === "structured" || d.type === "markdown";
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function applyToolDetail(
  tool: ToolResult,
  rawDetail: ToolDetail | string | undefined,
): ToolResult {
  if (rawDetail && typeof rawDetail === "object" && "type" in rawDetail) {
    return { ...tool, detail: rawDetail as ToolDetail };
  }
  if (rawDetail) {
    return {
      ...tool,
      detail: { type: "markdown", content: String(rawDetail) } as ToolDetail,
    };
  }
  return tool;
}

export function findMatchingToolIndex(
  session: Session,
  event: AgentEvent,
): { stepIndex: number; toolIndex: number } | null {
  for (const preferScoped of [true, false]) {
    for (let i = session.steps.length - 1; i >= 0; i--) {
      const step = session.steps[i];
      for (let j = step.tools.length - 1; j >= 0; j--) {
        const tool = step.tools[j];
        if (tool.name !== event.name) continue;
        // Only tools still awaiting a result can match — a finished tool
        // must never swallow a later same-name result.
        if (tool.status !== "running" && tool.status !== "pending") continue;
        if (preferScoped) {
          if (
            (event.callScope == null || tool.callScope === event.callScope) &&
            (event.callKind == null || tool.callKind === event.callKind)
          ) {
            return { stepIndex: i, toolIndex: j };
          }
        } else {
          return { stepIndex: i, toolIndex: j };
        }
      }
    }
  }
  return null;
}

export function pushRuntimeEvent(
  session: Session,
  listName: "guardEvents" | "hintEvents" | "modelEvents",
  event: AgentEvent,
): void {
  if (!session[listName]) session[listName] = [];
  const runtimeEvent: RuntimeEvent = { type: event.type as RuntimeEvent["type"] };
  if (event.layer !== undefined) runtimeEvent.layer = event.layer;
  if (event.guardName !== undefined) runtimeEvent.guardName = event.guardName;
  if (event.action !== undefined) runtimeEvent.action = event.action;
  if (event.reason !== undefined) runtimeEvent.reason = event.reason;
  if (event.hintType !== undefined) runtimeEvent.hintType = event.hintType;
  if (event.backend !== undefined) runtimeEvent.backend = event.backend;
  if (event.strategy !== undefined) runtimeEvent.strategy = event.strategy;
  if (event.terminationReason !== undefined)
    runtimeEvent.terminationReason = event.terminationReason;
  if (event.totalSteps !== undefined) runtimeEvent.totalSteps = event.totalSteps;
  session[listName].push(runtimeEvent);
}
