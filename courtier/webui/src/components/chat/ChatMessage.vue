<template>
  <UserMessage v-if="item.type === 'user'" :content="item.content" />
  <AssistantMessage
    v-else-if="item.type === 'assistant'"
    :content="item.content"
    :is-streaming="isStreaming"
  />
  <ThinkingCard
    v-else-if="item.type === 'thinking'"
    :content="item.content"
    :is-streaming="isStreaming"
    :default-open="item.isOpen"
  />
  <StepsMessage
    v-else-if="item.type === 'steps'"
    :steps="item.steps"
    :is-running="item.isRunning"
  />
  <FileCard
    v-else-if="item.type === 'file'"
    :file="item"
    @preview="$emit('preview', $event)"
  />
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
import ThinkingCard from "./ThinkingCard.vue";
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
}>();
</script>
