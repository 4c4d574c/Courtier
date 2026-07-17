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

export interface ChatThinkingItem {
  type: "thinking";
  id: string;
  content: string;
  isOpen: boolean;
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

export type ChatMessageItem =
  | ChatUserMessageItem
  | ChatAssistantMessageItem
  | ChatThinkingItem
  | ChatFileItem
  | ChatErrorItem
  | ChatStoppedItem
  | ChatGuardItem;

export interface ChatFileRecord {
  fileId: string;
  name: string;
  url: string;
}
