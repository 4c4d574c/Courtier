import { computed, type Ref } from "vue";
import type { Session } from "../types/agent";
import type { ChatFileRecord, ChatMessageItem } from "../types/chat";
import { buildChatMessages, deriveConversationTitle } from "../utils/chatMessages";

export function useChatMessages(
  session: Session,
  fileRecords: Ref<ChatFileRecord[]>,
) {
  const messages = computed<ChatMessageItem[]>(() =>
    buildChatMessages(session, fileRecords.value),
  );
  const title = computed(() => deriveConversationTitle(session));
  return { messages, title };
}
