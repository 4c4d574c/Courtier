<template>
  <UserMessage v-if="item.type === 'user'" :content="item.content" />
  <AssistantMessage
    v-else-if="item.type === 'assistant'"
    :content="item.content"
    :is-streaming="isStreaming"
    :citations="item.citations"
    @citation-click="$emit('citation-click', $event)"
  />
  <StepsMessage
    v-else-if="item.type === 'steps'"
    :groups="item.groups"
    :is-running="item.isRunning"
  />
  <div v-else-if="item.type === 'compacted'" class="compacted-row">
    <span v-if="item.pending" class="compacting-dot" />
    <template v-if="item.pending">{{ item.text }}</template>
    <template v-else>⟲ 上下文已压缩（{{ item.text }}）</template>
  </div>
  <div v-else-if="item.type === 'file'" class="file-row">
    <FileCard
      :file="item"
      @preview="$emit('preview', $event)"
    />
  </div>
  <ChatStatusMessage
    v-else-if="item.type === 'error' || item.type === 'stopped'"
    :type="item.type"
    :title="item.title"
    :detail="item.detail"
  />
  <GuardMessage
    v-else-if="item.type === 'guard'"
    :layer="item.layer"
    :guard-name="item.guardName"
    :action="item.action"
    :reason="item.reason"
  />
</template>

<script setup lang="ts">
import type { ChatMessageItem } from "../../types/chat";
import UserMessage from "./UserMessage.vue";
import AssistantMessage from "./AssistantMessage.vue";
import StepsMessage from "./StepsMessage.vue";
import FileCard from "./FileCard.vue";
import ChatStatusMessage from "./ChatStatusMessage.vue";
import GuardMessage from "./GuardMessage.vue";

interface Props {
  item: ChatMessageItem;
  isStreaming?: boolean;
}

defineProps<Props>();
defineEmits<{
  preview: [file: Extract<ChatMessageItem, { type: "file" }>];
  "citation-click": [hit: import("../../types/agent").CitationHit | undefined];
}>();
</script>

<style scoped>
.file-row {
  display: flex;
  justify-content: flex-end;
}

.compacted-row {
  display: flex;
  justify-content: center;
  align-items: center;
  gap: 6px;
  padding: 2px 0;
  font-size: 13px;
  color: var(--chat-text-tertiary);
  user-select: none;
}

.compacting-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--chat-text-tertiary);
  animation: compacting-pulse 1s ease-in-out infinite;
}

@keyframes compacting-pulse {
  0%,
  100% {
    opacity: 0.3;
    transform: scale(0.8);
  }
  50% {
    opacity: 1;
    transform: scale(1.2);
  }
}
</style>
