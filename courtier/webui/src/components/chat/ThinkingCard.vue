<template>
  <div class="thinking-card">
    <div
      class="thinking-card-header"
      role="button"
      tabindex="0"
      :aria-expanded="expanded"
      @click="toggle"
      @keydown.enter.prevent="toggle"
      @keydown.space.prevent="toggle"
    >
      <span class="thinking-card-label">
        <span v-if="isStreaming" class="thinking-card-dot"></span>
        {{ MESSAGES.CHAT_THINKING }}
        <span v-if="isStreaming" class="thinking-card-badge">
          {{ MESSAGES.CHAT_THINKING_ACTIVE }}
        </span>
      </span>
      <span class="thinking-card-toggle">{{ expanded ? "收起 ▲" : "展开 ▼" }}</span>
    </div>
    <Transition name="expand">
      <div v-if="expanded" class="thinking-card-body">
        <StreamingMarkdown
          class="thinking-card-text"
          :content="content"
          :is-streaming="!!isStreaming"
        />
      </div>
    </Transition>
  </div>
</template>

<script setup lang="ts">
import { ref } from "vue";
import { MESSAGES } from "../../constants/messages";
import StreamingMarkdown from "../StreamingMarkdown.vue";

interface Props {
  content: string;
  isStreaming?: boolean;
  defaultOpen?: boolean;
}

const props = defineProps<Props>();
const expanded = ref(props.defaultOpen ?? true);

function toggle() {
  expanded.value = !expanded.value;
}
</script>

<style scoped>
.thinking-card {
  border: 1px solid var(--chat-border);
  border-radius: var(--chat-radius-md);
  background: var(--chat-bg-card);
  overflow: hidden;
  box-shadow: var(--chat-shadow);
}

.thinking-card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 14px;
  cursor: pointer;
  user-select: none;
  background: var(--chat-bg-hover);
}

.thinking-card-label {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 14px;
  font-weight: 600;
  color: var(--chat-text-primary);
}

.thinking-card-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--chat-accent);
  animation: pulse 1.4s ease-in-out infinite;
}

.thinking-card-badge {
  font-size: 12px;
  color: var(--chat-accent);
  background: var(--chat-accent-soft);
  padding: 2px 8px;
  border-radius: 999px;
}

.thinking-card-toggle {
  font-size: 12px;
  color: var(--chat-text-secondary);
}

.thinking-card-body {
  padding: 14px 16px;
  background: var(--chat-bg-card);
}

.thinking-card-text {
  font-size: 14px;
  color: var(--chat-text-secondary);
  line-height: 1.7;
}

@keyframes pulse {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.3;
  }
}
</style>
