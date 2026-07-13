import type {
  Session,
  Step,
  SubagentRun,
  SubagentThought,
  ToolResult,
} from "../types/agent";

export const UNKNOWN_SUBAGENT_NAME = "未知子代理";

type ToolResultInput = Partial<ToolResult> & Pick<ToolResult, "id" | "name">;

export interface ParentToolDisplayItem {
  type: "tool";
  tool: ToolResult;
}

export interface SubagentToolDisplayItem {
  type: "subagent";
  key: string;
  name: string;
  wrapper?: ToolResult;
  tools: ToolResult[];
  isSynthetic: boolean;
  runStatus?: "running" | "completed" | "error";
  task?: string;
  conclusion?: string;
  thoughts?: SubagentThought[];
  error?: string;
  /** Nested sub-agent display items (regular tools + child sub-agents). */
  children: StepToolDisplayItem[];
}

export type StepToolDisplayItem =
  ParentToolDisplayItem | SubagentToolDisplayItem;

export function displayItemKey(item: StepToolDisplayItem): string {
  return item.type === "tool" ? `tool-${item.tool.id}` : `subagent-${item.key}`;
}

let _fallbackId = 0;

export function normalizeToolResult(tool: ToolResultInput): ToolResult {
  return {
    id: tool.id || `tool-legacy-${++_fallbackId}`,
    name: tool.name,
    skill: tool.skill ?? "",
    skillDescription: tool.skillDescription ?? "",
    status: tool.status ?? "done",
    callKind: tool.callKind ?? "tool",
    callScope: tool.callScope ?? "parent",
    subagentName: tool.subagentName ?? null,
    handleId: tool.handleId ?? null,
    parentHandleId: tool.parentHandleId ?? null,
    duration: tool.duration,
    summary: tool.summary,
    detail: tool.detail,
    startTime: tool.startTime,
    progress: tool.progress,
  };
}

export function normalizeStep(step: Step): Step {
  const normalizedTools = step.tools.map((tool) => normalizeToolResult(tool));
  const normalizedSubagents = (step.subagents ?? []).map((sa) =>
    normalizeSubagentRun(sa, normalizedTools),
  );
  return {
    ...step,
    tools: normalizedTools,
    subagents: normalizedSubagents,
  };
}

export function normalizeSubagentRun(
  sa: SubagentRun,
  stepTools?: ToolResult[],
): SubagentRun {
  const wrapper: ToolResult | undefined =
    sa.wrapper ??
    stepTools?.find(
      (t) => t.callKind === "subagent_run" && t.handleId === sa.handleId,
    );
  const normalizedTools = (sa.tools ?? []).map((t, i) =>
    normalizeToolResult({
      id: `subagent-tool-${sa.handleId}-${i}`,
      name: t.name,
      skill: t.skill ?? "",
      status: t.status ?? "done",
      callKind: t.callKind ?? ("tool" as const),
      callScope: "subagent" as const,
      subagentName: sa.name,
      handleId: t.handleId ?? sa.handleId,
      duration: t.duration,
      summary: t.summary,
    }),
  );
  const normalizedChildren = (sa.children ?? []).map((child) =>
    normalizeSubagentRun(child, stepTools),
  );
  return {
    ...sa,
    wrapper,
    tools: normalizedTools,
    children: normalizedChildren,
  };
}

export function normalizeSession(session: Session): Session {
  const steps = (session.steps ?? []).map(normalizeStep);
  let turns = (session.turns ?? []).map((turn) => ({
    ...turn,
    steps: (turn.steps ?? []).map(normalizeStep),
  }));

  // Backend to_detail_dict now returns turns with message + steps.
  // Fall back to synthesising a single Turn from flat steps for
  // sessions persisted before turn tracking was added.
  if (turns.length === 0 && steps.length > 0) {
    turns = [
      {
        message: {
          role: "user",
          text: session.task || "",
          timestamp: session.createdAt,
        },
        steps,
        conclusion: session.conclusion,
      },
    ];
  }

  return {
    ...session,
    steps,
    turns,
  };
}

export function displayToolName(tool: ToolResult): string {
  if (tool.callScope !== "subagent" || !tool.subagentName) return tool.name;

  const prefix = `[${tool.subagentName}] `;
  return tool.name.startsWith(prefix)
    ? tool.name.slice(prefix.length)
    : tool.name;
}

function buildSubagentChildItems(sa: SubagentRun): {
  items: StepToolDisplayItem[];
  referencedChildren: Set<string>;
} {
  const childMap = new Map<string, SubagentRun>();
  for (const child of sa.children ?? []) {
    childMap.set(child.handleId, child);
  }

  const items: StepToolDisplayItem[] = [];
  const referencedChildren = new Set<string>();

  for (const rawTool of sa.tools ?? []) {
    const tool = normalizeToolResult(rawTool);
    if (tool.callKind === "subagent_run") {
      const childHandleId = tool.handleId ?? tool.subagentName ?? tool.name;
      const childName = tool.subagentName ?? tool.name;
      const child = childMap.get(childHandleId);
      if (child) {
        referencedChildren.add(child.handleId);
        items.push(buildSubagentDisplayItem(child));
      } else {
        items.push({
          type: "subagent",
          key: `synthetic-${childHandleId}`,
          name: childName,
          wrapper: tool,
          tools: [],
          isSynthetic: true,
          children: [],
        });
      }
    } else {
      items.push({ type: "tool", tool });
    }
  }

  for (const child of sa.children ?? []) {
    if (!referencedChildren.has(child.handleId)) {
      items.push(buildSubagentDisplayItem(child));
    }
  }

  return { items, referencedChildren };
}

function buildSubagentDisplayItem(sa: SubagentRun): SubagentToolDisplayItem {
  const { items } = buildSubagentChildItems(sa);

  return {
    type: "subagent",
    key: sa.handleId,
    name: sa.name,
    wrapper: sa.wrapper,
    tools: (sa.tools ?? []).filter(
      (t) => normalizeToolResult(t).callKind !== "subagent_run",
    ),
    isSynthetic: false,
    runStatus: sa.status,
    task: sa.task,
    conclusion: sa.conclusion,
    thoughts: sa.thoughts,
    error: sa.error,
    children: items,
  };
}

export function buildStepToolGroups(
  tools: ToolResult[],
  subagents?: SubagentRun[],
): StepToolDisplayItem[] {
  const items: StepToolDisplayItem[] = [];
  const subagentIndexes = new Map<string, number>();
  const referencedSubagents = new Set<string>();
  const subagentMap = new Map<string, SubagentRun>();
  for (const sa of subagents ?? []) {
    subagentMap.set(sa.handleId, sa);
  }

  function ensureSubagentDisplayItem(
    name: string,
    handleId: string,
    wrapper?: ToolResult,
  ): SubagentToolDisplayItem {
    const idx = subagentIndexes.get(handleId);
    if (idx != null) {
      const existing = items[idx];
      if (existing.type === "subagent") return existing;
    }
    const sa = subagentMap.get(handleId);
    const item: SubagentToolDisplayItem = sa
      ? (referencedSubagents.add(handleId), buildSubagentDisplayItem(sa))
      : {
          type: "subagent",
          key: handleId,
          name,
          wrapper,
          tools: [],
          isSynthetic: !wrapper,
          children: [],
        };
    subagentIndexes.set(handleId, items.length);
    items.push(item);
    return item;
  }

  for (const rawTool of tools) {
    const tool = normalizeToolResult(rawTool);

    if (tool.callKind === "subagent_run") {
      const subagentName = tool.subagentName ?? tool.name;
      const handleId = tool.handleId ?? subagentName;
      const sa = subagentMap.get(handleId);
      if (sa) {
        referencedSubagents.add(handleId);
        items.push(buildSubagentDisplayItem(sa));
      } else {
        ensureSubagentDisplayItem(subagentName, handleId, tool);
      }
      continue;
    }

    if (tool.callScope === "subagent") {
      const subagentName = tool.subagentName ?? UNKNOWN_SUBAGENT_NAME;
      const handleId = tool.handleId ?? subagentName;
      const item = ensureSubagentDisplayItem(subagentName, handleId);
      const idx = subagentIndexes.get(handleId)!;
      items[idx] = { ...item, tools: [...item.tools, tool] };
      continue;
    }

    items.push({ type: "tool", tool });
  }

  for (const sa of subagents ?? []) {
    if (!referencedSubagents.has(sa.handleId)) {
      items.push(buildSubagentDisplayItem(sa));
    }
  }

  return items;
}
