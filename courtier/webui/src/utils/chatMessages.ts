import type { Session, Thought, Turn } from "../types/agent";
import type { ChatFileRecord, ChatMessageItem } from "../types/chat";
import { MESSAGES } from "../constants/messages";
import { thoughtsForStep } from "./sessionUtils";

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
  const ext = name.split(".").pop()?.toLowerCase();
  switch (ext) {
    case "pdf":
      return "application/pdf";
    case "jpg":
    case "jpeg":
      return "image/jpeg";
    case "png":
      return "image/png";
    case "gif":
      return "image/gif";
    case "bmp":
      return "image/bmp";
    case "tif":
    case "tiff":
      return "image/tiff";
    default:
      return "application/octet-stream";
  }
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

    items.push({
      type: "user",
      id: `${baseId}-user`,
      content: turn.message.text,
    });

    if (turn.message.fileName) {
      const record = findFileRecord(
        fileRecords,
        turn.message.fileId,
        turn.message.fileName,
      );
      items.push({
        type: "file",
        id: `${baseId}-file`,
        name: turn.message.fileName,
        url: record?.url ?? "",
        mimeType: record ? fileMimeType(record.name) : "application/octet-stream",
      });
    }

    const thoughts = collectTurnThoughts(turn, session.thoughts);
    if (thoughts.length > 0) {
      items.push({
        type: "thinking",
        id: `${baseId}-thinking`,
        content: thoughts.map((t) => t.text).join("\n\n"),
        isOpen: true,
      });
    }

    const conclusion = turn.conclusion ?? session.conclusion;
    if (conclusion || (isRunning && isLastTurn)) {
      items.push({
        type: "assistant",
        id: `${baseId}-assistant`,
        content: conclusion ?? "",
      });
    }

    if (isLastTurn) {
      if (session.errorMessage) {
        items.push({
          type: "error",
          id: `${baseId}-error`,
          title: MESSAGES.SESSION_ERROR_TITLE,
          detail: session.errorMessage,
        });
      } else if (session.stopReason === "user" && !isRunning) {
        items.push({
          type: "stopped",
          id: `${baseId}-stopped`,
          title: MESSAGES.SESSION_STOPPED,
          detail: MESSAGES.SESSION_STOPPED_DETAIL,
        });
      }
    }
  });

  return items;
}
