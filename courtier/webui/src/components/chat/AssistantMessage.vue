<template>
  <div class="assistant-message">
    <div class="assistant-message-bubble">
      <StreamingMarkdown
        class="assistant-message-text"
        :content="content"
        :is-streaming="!!isStreaming"
      />
    </div>
  </div>
</template>

<script setup lang="ts">
import StreamingMarkdown from "../StreamingMarkdown.vue";

interface Props {
  content: string;
  isStreaming?: boolean;
}
defineProps<Props>();
</script>

<style scoped>
.assistant-message {
  display: flex;
  justify-content: flex-start;
}

.assistant-message-bubble {
  max-width: 90%;
  background: var(--chat-bg-card);
  border: 1px solid var(--chat-border);
  border-radius: var(--chat-radius-lg);
  padding: 14px 18px;
  font-size: 15px;
  line-height: 1.7;
  color: var(--chat-text-primary);
  box-shadow: var(--chat-shadow);
}
</style>
