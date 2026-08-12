<template>
  <div class="assistant-message">
    <div class="assistant-message-bubble">
      <StreamingMarkdown
        class="assistant-message-text"
        :content="content"
        :is-streaming="!!isStreaming"
        @citation="onCitation"
      />
    </div>
  </div>
</template>

<script setup lang="ts">
import type { CitationHit } from "../../types/agent";
import StreamingMarkdown from "../StreamingMarkdown.vue";

interface Props {
  content: string;
  isStreaming?: boolean;
  /** search_documents hits referenced by `[[n]]` markers in content */
  citations?: CitationHit[];
}
const props = defineProps<Props>();

// Re-emitted with the resolved citation hit (or undefined when the marker
// index is out of range).
const emit = defineEmits<{ "citation-click": [hit: CitationHit | undefined] }>();

function onCitation(index: number) {
  const hit = props.citations?.[index - 1];
  if (!hit) {
    console.warn(`citation [[${index}]] out of range (${props.citations?.length ?? 0} hits)`);
  }
  emit("citation-click", hit);
}
</script>

<style scoped>
.assistant-message {
  display: flex;
  justify-content: flex-start;
}

.assistant-message-bubble {
  max-width: 100%;
  padding: 0 4px;
  font-size: 16px;
  line-height: 1.75;
  color: var(--chat-text-primary);
}
</style>
