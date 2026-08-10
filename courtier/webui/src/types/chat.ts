import type { Step, Thought } from "./agent";

export interface ChatUserMessageItem {
  type: "user";
  id: string;
  content: string;
}

export interface ChatAssistantMessageItem {
  type: "assistant";
  id: string;
  content: string;
}

export interface ChatFileItem {
  type: "file";
  id: string;
  name: string;
  url: string;
  mimeType: string;
}

export interface ChatErrorItem {
  type: "error";
  id: string;
  title: string;
  detail?: string;
}

export interface ChatStoppedItem {
  type: "stopped";
  id: string;
  title: string;
  detail?: string;
}

export interface ChatGuardItem {
  type: "guard";
  id: string;
  layer: string;
  guardName: string;
  action: "allow" | "log" | "block";
  reason?: string;
}

/** A step paired with the thinking that produced its tool calls. */
export interface StepThoughtGroup {
  step: Step;
  thoughts: Thought[];
}

/** A process block (rounded frame): consecutive steps with their thinking. */
export interface ChatStepsItem {
  type: "steps";
  id: string;
  groups: StepThoughtGroup[];
  isRunning: boolean;
}

/** Context compaction notice displayed inline in the chat flow. */
export interface ChatCompactedItem {
  type: "compacted";
  id: string;
  text: string;
  /** true 表示压缩仍在进行中（显示动态指示），false/缺省为完成态。 */
  pending?: boolean;
}

export type ChatMessageItem =
  | ChatUserMessageItem
  | ChatAssistantMessageItem
  | ChatFileItem
  | ChatErrorItem
  | ChatStoppedItem
  | ChatGuardItem
  | ChatCompactedItem
  | ChatStepsItem;

export interface ChatFileRecord {
  fileId: string;
  name: string;
  url: string;
}
