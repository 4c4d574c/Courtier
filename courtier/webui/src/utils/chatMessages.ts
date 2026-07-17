import type { Session, Thought, Turn } from "../types/agent";
import type {
  ChatFileRecord,
  ChatMessageItem,
  ChatUserMessageItem,
  ChatFileItem,
  ChatThinkingItem,
  ChatAssistantMessageItem,
  ChatErrorItem,
  ChatStoppedItem,
  ChatGuardItem,
} from "../types/chat";
import { MESSAGES } from "../constants/messages";
import { MIME_TYPES } from "../constants/fileUpload";
import { thoughtsForStep } from "./sessionUtils";

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

function collectTurnThoughts(turn: Turn, allThoughts: Thought[]): Thought[] {
  const seen = new Set<number>();
  const result: Thought[] = [];
  for (let i = 0; i < turn.steps.length; i++) {
    for (const thought of thoughtsForStep(allThoughts, turn, i)) {
      if (!seen.has(thought.id)) {
        seen.add(thought.id);
        result.push(thought);
      }
    }
  }
  return result;
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

function buildUserItem(turn: Turn, baseId: string): ChatUserMessageItem {
  return {
    type: "user",
    id: `${baseId}-user`,
    content: turn.message.text,
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

function buildThinkingItem(
  turn: Turn,
  baseId: string,
  allThoughts: Thought[],
): ChatThinkingItem | null {
  const thoughts = collectTurnThoughts(turn, allThoughts);
  if (thoughts.length === 0) return null;
  return {
    type: "thinking",
    id: `${baseId}-thinking`,
    content: thoughts.map((t) => t.text).join("\n\n"),
    isOpen: true,
  };
}

function buildAssistantItem(
  turn: Turn,
  baseId: string,
  sessionConclusion: string | undefined,
  isRunning: boolean,
  isLastTurn: boolean,
): ChatAssistantMessageItem | null {
  const conclusion = turn.conclusion ?? sessionConclusion;
  if (!conclusion && !(isRunning && isLastTurn)) return null;
  return {
    type: "assistant",
    id: `${baseId}-assistant`,
    content: conclusion ?? "",
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

    items.push(buildUserItem(turn, baseId));

    const fileItem = buildFileItem(turn, baseId, fileRecords);
    if (fileItem) items.push(fileItem);

    const thinkingItem = buildThinkingItem(turn, baseId, session.thoughts);
    if (thinkingItem) items.push(thinkingItem);

    const assistantItem = buildAssistantItem(
      turn,
      baseId,
      session.conclusion,
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
