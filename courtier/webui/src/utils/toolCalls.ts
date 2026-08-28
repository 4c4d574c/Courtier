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
  displayName?: string | null;
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

function generateFallbackToolId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return `tool-legacy-${crypto.randomUUID()}`;
  }
  return `tool-legacy-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

// normalizeToolResult is pure and its inputs are never mutated in place
// (mutators always spread into a fresh object), so outputs can be memoized
// per input identity: step patches rebuild the tail step's tools array per
// tool event and unchanged tools then reuse the same normalized object,
// letting Vue skip re-rendering finished tool cards. Also stabilizes the
// generated fallback id for id-less tools across rebuilds.
const normalizeMemo = new WeakMap<ToolResultInput, ToolResult>();

export function normalizeToolResult(tool: ToolResultInput): ToolResult {
  const memoHit = normalizeMemo.get(tool);
  if (memoHit) return memoHit;
  const normalized: ToolResult = {
    id: tool.id || generateFallbackToolId(),
    name: tool.name,
    displayName: tool.displayName ?? null,
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
    issueCounts: tool.issueCounts,
    detail: tool.detail,
    toolCallId: tool.toolCallId ?? null,
    citationOffset: tool.citationOffset ?? null,
    startTime: tool.startTime,
    progress: tool.progress,
  };
  normalizeMemo.set(tool, normalized);
  return normalized;
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
      displayName: t.displayName ?? null,
      skill: t.skill ?? "",
      status: t.status ?? "done",
      callKind: t.callKind ?? ("tool" as const),
      callScope: "subagent" as const,
      subagentName: sa.name,
      handleId: t.handleId ?? sa.handleId,
      duration: t.duration,
      summary: t.summary,
      issueCounts: t.issueCounts,
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

export function isDeprecatedTool(tool: ToolResult): boolean {
  const text = `${tool.name} ${tool.skillDescription ?? ""}`;
  return text.includes("[DEPRECATED");
}

export function parseDeprecatedReplacement(
  tool: ToolResult,
): string | undefined {
  const text = tool.skillDescription || tool.name || "";
  const match = text.match(/\[DEPRECATED(?:\s*\(use\s+([^)]+)\))?\]/i);
  if (!match) return undefined;
  return match[1];
}

function buildSubagentChildItems(sa: SubagentRun): {
  items: StepToolDisplayItem[];
  referencedChildren: Set<string>;
} {
  const childMap = new Map<string, SubagentRun>();
  for (const child of sa.children ?? []) {
    childMap.set(child.handleId, child);
  }
  // Name index for legacy dispatch records persisted before the backend
  // marked callKind="subagent_run" — those still read "tool".
  const childByName = new Map<string, SubagentRun>();
  for (const child of sa.children ?? []) {
    if (!childByName.has(child.name)) childByName.set(child.name, child);
  }

  const items: StepToolDisplayItem[] = [];
  const referencedChildren = new Set<string>();

  for (const rawTool of sa.tools ?? []) {
    const tool = normalizeToolResult(rawTool);
    if (tool.callKind === "subagent_run") {
      const childHandleId = tool.handleId ?? tool.subagentName ?? tool.name;
      const childName = tool.subagentName ?? tool.name;
      const child = childMap.get(childHandleId) ?? childByName.get(childName);
      if (child) {
        referencedChildren.add(child.handleId);
        // Attach the dispatch record as the wrapper so the aggregated
        // result/duration stay on the sub-agent node.
        items.push(
          buildSubagentDisplayItem({
            ...child,
            wrapper: child.wrapper ?? tool,
          }),
        );
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
      continue;
    }
    // Legacy dispatch record (callKind "tool") whose name matches a child
    // run is that child's wrapper — merging here prevents a duplicate
    // standalone tool card next to the sub-agent tree node.
    const matchedChild = childByName.get(tool.name);
    if (matchedChild) {
      referencedChildren.add(matchedChild.handleId);
      items.push(
        buildSubagentDisplayItem({
          ...matchedChild,
          wrapper: matchedChild.wrapper ?? tool,
        }),
      );
      continue;
    }
    items.push({ type: "tool", tool });
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
    displayName: sa.displayName ?? null,
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
  const subagentByName = new Map<string, SubagentRun>();
  for (const sa of subagents ?? []) {
    subagentMap.set(sa.handleId, sa);
    subagentByName.set(sa.name, sa);
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
      let sa = subagentMap.get(handleId);
      if (!sa && !tool.handleId) {
        // Pending wrapper before tool_result (handleId is null): fall back
        // to name matching so the tool card and the sub-agent tree do not
        // render twice while the skill is still running.
        sa = (subagents ?? []).find((r) => r.name === subagentName);
      }
      if (sa) {
        referencedSubagents.add(sa.handleId);
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

    // A parent-scope tool whose name matches an active sub-agent run is that
    // run's pending wrapper (callKind is only classified at tool_result time,
    // so during the run it still reads "tool"). Merge it instead of showing
    // a duplicate standalone card next to the sub-agent tree.
    const matchedRun = subagentByName.get(tool.name);
    if (matchedRun) {
      referencedSubagents.add(matchedRun.handleId);
      items.push(
        buildSubagentDisplayItem({ ...matchedRun, wrapper: tool }),
      );
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
