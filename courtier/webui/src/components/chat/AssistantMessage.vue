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
  max-width: 100%;
  padding: 0 4px;
  font-size: 15px;
  line-height: 1.75;
  color: var(--chat-text-primary);
}
</style>
