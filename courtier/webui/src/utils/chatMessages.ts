import type { CitationHit, Session, Thought, Turn } from "../types/agent";
import type {
  CitationIndex,
  ChatFileRecord,
  ChatMessageItem,
  ChatUserMessageItem,
  ChatFileItem,
  ChatAssistantMessageItem,
  ChatErrorItem,
  ChatStoppedItem,
  ChatGuardItem,
  StepThoughtGroup,
} from "../types/chat";
import { MESSAGES } from "../constants/messages";
import { MIME_TYPES } from "../constants/fileUpload";
import { thoughtsForSteps } from "./sessionUtils";

const MIME_TYPE_MAP: Record<string, string> = Object.fromEntries(
  Object.entries(MIME_TYPES).map(([ext, mimes]) => [
    ext,
    mimes[0] || "application/octet-stream",
  ]),
);

export function deriveConversationTitle(session: Session): string {
  if (session.task?.trim()) {
    return truncate(session.task.trim(), 20);
  }
  const firstUserText = session.turns.find(
    (t) => t.message.text.trim(),
  )?.message.text;
  if (firstUserText) {
    return truncate(firstUserText.trim(), 20);
  }
  return "新会话";
}

function truncate(text: string, max: number): string {
  if (text.length <= max) return text;
  return text.slice(0, max) + "…";
}

function findFileRecord(
  records: ChatFileRecord[],
  fileId?: string,
  fileName?: string,
): ChatFileRecord | undefined {
  if (fileId) {
    const byId = records.find((r) => r.fileId === fileId);
    if (byId) return byId;
  }
  if (fileName) {
    return records.find((r) => r.name === fileName);
  }
  return undefined;
}

export function fileMimeType(name: string): string {
  const ext = `.${name.split(".").pop()?.toLowerCase() || ""}`;
  return MIME_TYPE_MAP[ext] || "application/octet-stream";
}

function buildUserItem(
  turn: Turn,
  baseId: string,
  turnIndex: number,
): ChatUserMessageItem {
  return {
    type: "user",
    id: `${baseId}-user`,
    content: turn.message.text,
    turnIndex,
  };
}

function buildFileItem(
  turn: Turn,
  baseId: string,
  records: ChatFileRecord[],
): ChatFileItem | null {
  if (!turn.message.fileName) return null;
  const record = findFileRecord(records, turn.message.fileId, turn.message.fileName);
  return {
    type: "file",
    id: `${baseId}-file`,
    name: turn.message.fileName,
    url: record?.url ?? "",
    mimeType: record ? fileMimeType(record.name) : "application/octet-stream",
  };
}

/**
 * Build the turn's process blocks (rounded frames).
 *
 * Thinking is split per step: each step carries the thoughts that produced
 * its tool calls, rendered above those tool cards inside the frame.  A step
 * verdict is intermediate assistant text (a non-thinking intermediate
 * process) produced between the thinking and the tool calls of the same
 * think, so the step splits there: its thinking stays in the frame above,
 * the text closes that frame and renders outside it, and its tool calls
 * open a new frame below the text.
 */
function buildProcessItems(
  turn: Turn,
  baseId: string,
  allThoughts: Thought[],
  isRunning: boolean,
  pendingVerdict?: string,
  pendingVerdictAfterStepIndex?: number,
): ChatMessageItem[] {
  const items: ChatMessageItem[] = [];
  let groups: StepThoughtGroup[] = [];
  let block = 0;

  const flush = () => {
    if (groups.length === 0) return;
    items.push({
      type: "steps",
      id: `${baseId}-steps-${block}`,
      groups,
      isRunning: false,
    });
    block += 1;
    groups = [];
  };

  // 运行中轮次：已流式收到但尚未定性的中间文本，按"待定 verdict"即时
  // 渲染。边界 = 当前 think 对应 step 的前一个 step 的 index（文本将
  // 转正为该 step 的 verdict，渲染在它的工具框之前）；边界之后创建的
  // step 渲染在文本下方的新框中。observe 时 step_verdict 转正，布局
  // 不变、无跳变；轮次结束则剩余文本成为最终结论。
  const pendingText = isRunning ? (pendingVerdict ?? "") : "";
  const hasPending = pendingText.trim() !== "";
  const boundary = pendingVerdictAfterStepIndex ?? 0;
  let pendingPushed = false;
  const pushPending = () => {
    if (!hasPending || pendingPushed) return;
    flush();
    items.push({
      type: "assistant",
      id: `${baseId}-pending-verdict`,
      content: pendingText,
    });
    pendingPushed = true;
  };

  // One pass over the turn's thoughts — the per-step scan inside the loop
  // was the dominant per-token rebuild cost under deep reactivity.
  const stepThoughtLists = thoughtsForSteps(turn, allThoughts);

  turn.steps.forEach((step, stepIndexInTurn) => {
    const thoughts = (stepThoughtLists[stepIndexInTurn] ?? []).filter((t) =>
      t.text.trim() !== "",
    );
    const hasVerdict = !!step.verdict?.trim();
    const isPendingSplit = hasPending && !pendingPushed && step.index > boundary;

    if (!hasVerdict && !isPendingSplit) {
      groups.push({ step, thoughts });
      return;
    }

    // 文本由当前 think 在思考之后、工具调用之前产生，因此该 step 在此
    // 处拆分：思考（先于文本产生）留在上方框；文本闭合上框、渲染在框外；
    // 工具与子代理在文本下方开新框。无思考/无工具时不产生空组；纯思考组
    // 必须同时摘掉 tools 和 subagents，否则子代理会在上下两框重复渲染。
    if (thoughts.length > 0) {
      groups.push({ step: { ...step, tools: [], subagents: [] }, thoughts });
    }
    flush();
    if (hasVerdict) {
      items.push({
        type: "assistant",
        id: `${baseId}-verdict-${step.index}`,
        content: step.verdict!,
      });
    } else {
      pushPending();
    }
    if (step.tools.length > 0 || (step.subagents?.length ?? 0) > 0) {
      groups.push({ step, thoughts: [] });
    }
  });
  flush();
  // 边界之后尚无 step（文本正在流式、工具调用未到）：渲染在所有框之后，
  // 即未来新框的上方。
  pushPending();

  // Only the final frame of the turn can still be streaming.
  for (let i = items.length - 1; i >= 0; i--) {
    const item = items[i];
    if (item.type === "steps") {
      item.isRunning = isRunning;
      break;
    }
  }
  return items;
}

/**
 * Find the citations attached to a turn's search_documents results.
 *
 * A turn may run several search_documents calls (e.g. one per topic).  The
 * backend numbers hits cumulatively across the calls of the turn; each
 * tool result carries citationOffset (the absolute index of its first
 * hit), so `byNumber` maps `[[n]]` directly regardless of event order.
 * Older events without offsets fall back to merge order.
 */
function citationsForTurn(turn: Turn): CitationIndex | undefined {
  const byNumber = new Map<number, CitationHit>();
  const list: CitationHit[] = [];
  for (const step of turn.steps) {
    for (const tool of step.tools) {
      if (tool.name !== "search_documents" || !tool.citations?.length) continue;
      const offset = tool.citationOffset;
      tool.citations.forEach((hit, i) => {
        if (offset != null) {
          byNumber.set(offset + i + 1, hit);
        } else {
          byNumber.set(list.length + 1, hit);
        }
        list.push(hit);
      });
    }
  }
  return list.length ? { byNumber, list } : undefined;
}

function buildAssistantItem(
  turn: Turn,
  baseId: string,
  isRunning: boolean,
  isLastTurn: boolean,
): ChatAssistantMessageItem | null {
  // Only the turn's own conclusion — no session-level fallback.  Restored
  // turns always carry a string ("" = none) from the backend; a live turn
  // without one either is still streaming (empty placeholder block, filled
  // via the pending-verdict item) or terminated without producing text
  // (error/stopped).  Falling back to session.conclusion there would render
  // the PREVIOUS turn's conclusion as this turn's.
  const conclusion = turn.conclusion ?? "";
  if (!conclusion && !(isRunning && isLastTurn)) return null;
  const citations = citationsForTurn(turn);
  return {
    type: "assistant",
    id: `${baseId}-assistant`,
    content: conclusion,
    ...(citations ? { citations } : {}),
  };
}

function buildStatusItem(
  session: Session,
  baseId: string,
  isRunning: boolean,
  isLastTurn: boolean,
): ChatErrorItem | ChatStoppedItem | null {
  if (!isLastTurn) return null;
  if (session.errorMessage) {
    return {
      type: "error",
      id: `${baseId}-error`,
      title: MESSAGES.SESSION_ERROR_TITLE,
      detail: session.errorMessage,
    };
  }
  if (session.stopReason === "user" && !isRunning) {
    return {
      type: "stopped",
      id: `${baseId}-stopped`,
      title: MESSAGES.SESSION_STOPPED,
      detail: MESSAGES.SESSION_STOPPED_DETAIL,
    };
  }
  return null;
}

function buildGuardItems(
  session: Session,
  baseId: string,
  isLastTurn: boolean,
): ChatGuardItem[] {
  if (!isLastTurn || !session.guardEvents?.length) return [];
  return session.guardEvents.map((event, index) => ({
    type: "guard",
    id: `${baseId}-guard-${index}`,
    layer: event.layer || "unknown",
    guardName: event.guardName || "UnknownGuard",
    action: event.action || "log",
    reason: event.reason,
  }));
}

export function buildChatMessages(
  session: Session,
  fileRecords: ChatFileRecord[],
): ChatMessageItem[] {
  const items: ChatMessageItem[] = [];
  const isRunning = session.status === "running";

  session.turns.forEach((turn, turnIndex) => {
    const baseId = `turn-${turnIndex}`;
    const isLastTurn = turnIndex === session.turns.length - 1;

    const fileItem = buildFileItem(turn, baseId, fileRecords);
    if (fileItem) items.push(fileItem);

    items.push(buildUserItem(turn, baseId, turnIndex));

    items.push(
      ...buildProcessItems(
        turn,
        baseId,
        session.thoughts,
        isRunning && isLastTurn,
        isLastTurn ? session.pendingVerdict : undefined,
        isLastTurn ? session.pendingVerdictAfterStepIndex : undefined,
      ),
    );

    // Context-compaction notices for this turn go right after its process
    // blocks (they happened mid-run, before the conclusion).
    for (const [i, notice] of (session.compactions ?? [])
      .filter((n) => n.turnIndex === turnIndex + 1)
      .entries()) {
      items.push({
        type: "compacted",
        id: `${baseId}-compacted-${i}`,
        text: notice.text,
      });
    }

    // Compaction currently in progress: show a live indicator at the tail
    // of the running turn (replaced by the compacted notice when done).
    if (isRunning && isLastTurn && session.compacting) {
      items.push({
        type: "compacted",
        id: `${baseId}-compacting`,
        text: MESSAGES.CHAT_COMPACTING,
        pending: true,
      });
    }

    const assistantItem = buildAssistantItem(
      turn,
      baseId,
      isRunning,
      isLastTurn,
    );
    if (assistantItem) items.push(assistantItem);

    const statusItem = buildStatusItem(session, baseId, isRunning, isLastTurn);
    if (statusItem) items.push(statusItem);

    const guardItems = buildGuardItems(session, baseId, isLastTurn);
    items.push(...guardItems);
  });

  return items;
}
