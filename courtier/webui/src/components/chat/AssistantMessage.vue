<template>
  <div class="assistant-message">
    <div class="assistant-message-body">
      <div class="assistant-message-bubble">
        <StreamingMarkdown
          class="assistant-message-text"
          :content="content"
          :is-streaming="!!isStreaming"
          @citation="onCitation"
        />
      </div>
      <div v-if="showActions" class="assistant-message-actions">
        <button
          type="button"
          class="assistant-message-action"
          @click="copyContent"
        >
          {{ copied ? MESSAGES.CHAT_COPIED : MESSAGES.CHAT_COPY }}
        </button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from "vue";
import type { CitationHit } from "../../types/agent";
import type { CitationIndex } from "../../types/chat";
import { MESSAGES } from "../../constants/messages";
import StreamingMarkdown from "../StreamingMarkdown.vue";
import {
  buildConclusionCopy,
  writeRichClipboard,
} from "../../utils/conclusionCopy";

interface Props {
  content: string;
  isStreaming?: boolean;
  /** search_documents hits referenced by `[[n]]` markers in content */
  citations?: CitationIndex;
}
const props = defineProps<Props>();

// Re-emitted with the resolved citation hit (or undefined when the marker
// index is out of range).
const emit = defineEmits<{ "citation-click": [hit: CitationHit | undefined] }>();

function onCitation(index: number) {
  const c = props.citations;
  const hit = c?.byNumber.get(index) ?? c?.list[index - 1];
  if (!hit) {
    console.warn(
      `citation [[${index}]] out of range (${c?.list.length ?? 0} hits)`,
    );
  }
  emit("citation-click", hit);
}

// Hidden while streaming: the conclusion is still growing and a mid-flight
// copy would capture partial text.
const showActions = computed(
  () => !props.isStreaming && props.content.length > 0,
);

// ---- copy ----

const copied = ref(false);
let copiedTimer: ReturnType<typeof setTimeout> | undefined;

async function copyContent() {
  await writeRichClipboard(buildConclusionCopy(props.content, props.citations));
  copied.value = true;
  clearTimeout(copiedTimer);
  copiedTimer = setTimeout(() => {
    copied.value = false;
  }, 1500);
}

onBeforeUnmount(() => clearTimeout(copiedTimer));
</script>

<style scoped>
.assistant-message {
  display: flex;
  justify-content: flex-start;
}

.assistant-message-body {
  max-width: 100%;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
}

.assistant-message-bubble {
  max-width: 100%;
  padding: 0 4px;
  font-size: 16px;
  line-height: 1.75;
  color: var(--chat-text-primary);
}

/* Hover-revealed action row, mirroring UserMessage's copy pattern.  The row
   is always rendered so the layout never shifts; it only becomes visible and
   clickable on hover. */
.assistant-message-actions {
  display: flex;
  gap: 4px;
  margin-top: 2px;
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.15s;
}

.assistant-message-body:hover .assistant-message-actions,
.assistant-message-actions:focus-within {
  opacity: 1;
  pointer-events: auto;
}

@media (hover: none) {
  .assistant-message-actions {
    opacity: 1;
    pointer-events: auto;
  }
}

.assistant-message-action {
  border: none;
  background: none;
  padding: 2px 6px;
  font-size: 13px;
  color: var(--chat-text-tertiary);
  cursor: pointer;
  border-radius: var(--chat-radius-md, 6px);
  transition: color 0.15s;
}

.assistant-message-action:hover {
  color: var(--chat-text-primary);
}
</style>
