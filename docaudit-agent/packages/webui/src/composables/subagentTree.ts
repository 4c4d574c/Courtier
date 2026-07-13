import type { SubagentRun, SubagentThought, ToolResult } from "../types/agent";

export interface PendingSubagent {
  run: SubagentRun;
  parentHandleId?: string | null;
}

export function findNodeByHandleId(
  roots: SubagentRun[],
  handleId: string,
): SubagentRun | undefined {
  for (const root of roots) {
    if (root.handleId === handleId) return root;
    if (root.children) {
      const childMatch = findNodeByHandleId(root.children, handleId);
      if (childMatch) return childMatch;
    }
  }
  return undefined;
}

export function findRunningParentByHandleId(
  roots: SubagentRun[],
  parentHandleId: string,
): SubagentRun | undefined {
  const node = findNodeByHandleId(roots, parentHandleId);
  if (node?.status === "running") return node;
  return undefined;
}

export function updateNodeInTree(
  roots: SubagentRun[],
  handleId: string,
  updater: (node: SubagentRun) => SubagentRun,
): SubagentRun[] {
  return roots.map((root) => {
    if (root.handleId === handleId) return updater(root);
    if (root.children) {
      return {
        ...root,
        children: updateNodeInTree(root.children, handleId, updater),
      };
    }
    return root;
  });
}

export function upsertNodeInContainer(
  container: SubagentRun[],
  patch: Partial<SubagentRun> & { name: string; handleId: string },
): SubagentRun[] {
  const { name, handleId, ...patchRest } = patch;
  const idx = container.findIndex((sa) => sa.handleId === handleId);
  if (idx >= 0) {
    const existing = container[idx];
    const updated: SubagentRun = {
      ...existing,
      ...patchRest,
      name,
      handleId,
      conclusion:
        patchRest.conclusion !== undefined
          ? (existing.conclusion ?? "") + patchRest.conclusion
          : existing.conclusion,
    };
    return container.map((sa, i) => (i === idx ? updated : sa));
  }
  const created: SubagentRun = {
    name,
    handleId,
    task: patchRest.task ?? "",
    status: patchRest.status ?? "running",
    ...patchRest,
  };
  return [...container, created];
}

export function upsertNodeInTree(
  roots: SubagentRun[],
  name: string,
  handleId: string,
  parentHandleId: string | null | undefined,
  patch: Partial<SubagentRun>,
): SubagentRun[] {
  if (parentHandleId) {
    const parent = findRunningParentByHandleId(roots, parentHandleId);
    if (parent) {
      return updateNodeInTree(roots, parent.handleId, (p) => ({
        ...p,
        children: upsertNodeInContainer(p.children ?? [], {
          name,
          handleId,
          ...patch,
        }),
      }));
    }
  }
  return upsertNodeInContainer(roots, { name, handleId, ...patch });
}

export function appendSubagentThoughtInContainer(
  container: SubagentRun[],
  handleId: string,
  text: string,
  nextId: number,
): SubagentRun[] {
  const idx = container.findIndex((sa) => sa.handleId === handleId);
  if (idx < 0) return container;
  const existing = container[idx];
  const blocks: SubagentThought[] = existing.thoughts ?? [];
  const nextBlocks: SubagentThought[] =
    blocks.length === 0
      ? [{ id: nextId, text }]
      : [
          ...blocks.slice(0, -1),
          {
            ...blocks[blocks.length - 1],
            text: blocks[blocks.length - 1].text + text,
          },
        ];
  const updated: SubagentRun = { ...existing, thoughts: nextBlocks };
  return container.map((sa, i) => (i === idx ? updated : sa));
}

export function newSubagentThoughtBlockInContainer(
  container: SubagentRun[],
  handleId: string,
  nextId: number,
): SubagentRun[] {
  const idx = container.findIndex((sa) => sa.handleId === handleId);
  if (idx < 0) return container;
  const existing = container[idx];
  const updated: SubagentRun = {
    ...existing,
    thoughts: [...(existing.thoughts ?? []), { id: nextId, text: "" }],
  };
  return container.map((sa, i) => (i === idx ? updated : sa));
}

export function addSubagentToolInContainer(
  container: SubagentRun[],
  handleId: string,
  tool: ToolResult,
): SubagentRun[] {
  const idx = container.findIndex((sa) => sa.handleId === handleId);
  if (idx < 0) return container;
  const existing = container[idx];
  const existingTools = existing.tools ?? [];
  const existingToolIdx = existingTools.findIndex(
    (t) => t.id === tool.id || (t.name === tool.name && t.status === "running"),
  );
  const nextTools =
    existingToolIdx >= 0
      ? existingTools.map((t, i) =>
          i === existingToolIdx ? { ...t, ...tool } : t,
        )
      : [...existingTools, tool];
  const updated: SubagentRun = { ...existing, tools: nextTools };
  return container.map((sa, i) => (i === idx ? updated : sa));
}

export function routeSubagentWrapper(
  roots: SubagentRun[],
  name: string,
  handleId: string,
  parentHandleId: string | null | undefined,
  wrapper: ToolResult,
): SubagentRun[] {
  const withWrapper = upsertNodeInTree(roots, name, handleId, parentHandleId, {
    wrapper,
  });
  if (!parentHandleId) return withWrapper;
  const parent = findRunningParentByHandleId(withWrapper, parentHandleId);
  if (!parent) return withWrapper;
  return updateNodeInTree(withWrapper, parent.handleId, (p) => ({
    ...p,
    tools: [...(p.tools ?? []), wrapper],
  }));
}
