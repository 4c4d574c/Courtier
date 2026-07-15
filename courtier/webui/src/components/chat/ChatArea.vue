<template>
  <div ref="containerRef" class="chat-area" aria-live="polite">
    <div v-if="messages.length === 0 && !isRunning" class="chat-welcome">
      <div class="chat-welcome-mark">审</div>
      <h2 class="chat-welcome-title">{{ MESSAGES.WELCOME_TITLE }}</h2>
      <p class="chat-welcome-desc">{{ MESSAGES.WELCOME_DESC }}</p>
      <div class="chat-welcome-examples">
        <button
          v-for="example in examples"
          :key="example.task"
          class="chat-welcome-example"
          type="button"
          @click="$emit('quick-task', example.task)"
        >
          {{ example.label }}
        </button>
      </div>
    </div>

    <div v-else-if="messages.length === 0 && isRunning" class="chat-loading">
      <div class="chat-loading-dot"></div>
      <p>{{ MESSAGES.LOADING_CONNECTING }}</p>
    </div>

    <template v-else>
      <ChatMessage
        v-for="(item, index) in messages"
        :key="item.id"
        :item="item"
        :is-streaming="isRunning && index === messages.length - 1"
        @preview="$emit('preview', $event)"
      />
    </template>
  </div>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, watch, nextTick } from "vue";
import type { ChatMessageItem } from "../../types/chat";
import { MESSAGES } from "../../constants/messages";
import { useAutoScroll } from "../../composables/useAutoScroll";
import ChatMessage from "./ChatMessage.vue";

interface Props {
  messages: ChatMessageItem[];
  isRunning: boolean;
}

const props = defineProps<Props>();
defineEmits<{
  "quick-task": [task: string];
  preview: [file: Extract<ChatMessageItem, { type: "file" }>];
}>();

const examples = [
  { label: MESSAGES.QUICK_FORMAT, task: "审核这份通知的格式规范" },
  { label: MESSAGES.QUICK_CONTENT, task: "检查公文内容是否符合规范要求" },
  { label: MESSAGES.QUICK_FULL, task: "全面审核这份文档" },
];

const { containerRef, mount, unmount, forceScrollToBottom, scrollToBottom } =
  useAutoScroll();

onMounted(() => mount());
onUnmounted(() => unmount());

watch(
  () => props.messages.length,
  () => nextTick(forceScrollToBottom),
);

watch(
  () => props.isRunning,
  () => nextTick(scrollToBottom),
);
</script>

<style scoped>
.chat-area {
  flex: 1;
  overflow-y: auto;
  padding: 24px 16px;
  display: flex;
  flex-direction: column;
  gap: 16px;
  background: var(--chat-bg-body);
}

.chat-welcome,
.chat-loading {
  margin: auto;
  display: flex;
  flex-direction: column;
  align-items: center;
  text-align: center;
  gap: 14px;
  padding: 48px 24px;
}

.chat-welcome-mark {
  width: 64px;
  height: 64px;
  border: 2px solid var(--chat-accent);
  border-radius: var(--chat-radius-md);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 28px;
  font-weight: 700;
  color: var(--chat-accent);
}

.chat-welcome-title {
  font-size: 22px;
  font-weight: 600;
  color: var(--chat-text-primary);
  margin: 0;
}

.chat-welcome-desc {
  font-size: 15px;
  color: var(--chat-text-secondary);
  margin: 0;
}

.chat-welcome-examples {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  justify-content: center;
  margin-top: 8px;
}

.chat-welcome-example {
  padding: 8px 16px;
  border: 1px solid var(--chat-border);
  border-radius: var(--chat-radius-md);
  background: var(--chat-bg-card);
  color: var(--chat-text-secondary);
  font-size: 14px;
  cursor: pointer;
  transition:
    border-color 0.15s,
    color 0.15s;
}

.chat-welcome-example:hover {
  border-color: var(--chat-accent);
  color: var(--chat-accent);
}

.chat-loading-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: var(--chat-accent);
  animation: pulse 1.2s ease-in-out infinite;
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
