<template>
  <div ref="containerRef" class="chat-area" aria-live="polite">
    <div v-if="messages.length === 0 && !isRunning" class="chat-welcome">
      <div class="chat-welcome-mark"><img :src="logoUrl" alt="审衡" /></div>
      <h2 class="chat-welcome-title">{{ MESSAGES.WELCOME_TITLE }}</h2>
      <p class="chat-welcome-desc">{{ MESSAGES.WELCOME_DESC }}</p>
    </div>

    <div v-else-if="messages.length === 0 && isRunning" class="chat-loading">
      <template v-if="queuePosition !== undefined">
        <div class="chat-loading-dot chat-loading-dot--queued"></div>
        <p>
          {{ queuePosition > 0 ? `排队中，前面还有 ${queuePosition} 个任务` : "排队中，即将开始" }}
        </p>
      </template>
      <template v-else>
        <div class="chat-loading-dot"></div>
        <p>{{ MESSAGES.LOADING_CONNECTING }}</p>
      </template>
    </div>

    <template v-else>
      <ChatMessage
        v-for="(item, index) in messages"
        :key="item.id"
        :item="item"
        :is-streaming="isRunning && index === messages.length - 1"
        :can-edit="canEdit"
        :edit-hint="editHint"
        @preview="$emit('preview', $event)"
        @citation-click="$emit('citation-click', $event)"
        @edit-submit="$emit('edit-submit', $event)"
      />
      <div v-if="isRunning && queuePosition !== undefined" class="chat-queue-banner">
        {{ queuePosition > 0 ? `排队中，前面还有 ${queuePosition} 个任务` : "排队中，即将开始" }}
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, watch, nextTick } from "vue";
import type { ChatMessageItem } from "../../types/chat";
import { MESSAGES } from "../../constants/messages";
import { useAutoScroll } from "../../composables/useAutoScroll";
import ChatMessage from "./ChatMessage.vue";
import logoUrl from "../../assets/logo.png";

interface Props {
  messages: ChatMessageItem[];
  isRunning: boolean;
  /** Server-side queue position (tasks ahead); undefined = not queued. */
  queuePosition?: number;
  /** User-bubble edit entry: false hides it (running session). */
  canEdit?: boolean;
  /** Non-empty disables editing with the reason shown as tooltip. */
  editHint?: string;
}

const props = defineProps<Props>();
defineEmits<{
  preview: [file: Extract<ChatMessageItem, { type: "file" }>];
  "citation-click": [hit: import("../../types/agent").CitationHit | undefined];
  "edit-submit": [payload: { turnIndex: number; text: string }];
}>();

const { containerRef, mount, unmount, scrollToBottom } = useAutoScroll();

onMounted(() => mount());
onUnmounted(() => unmount());

watch(
  // Lightweight trigger: deep-watching the whole array would re-traverse
  // every message on each streamed token. Track count + last content size.
  () => {
    const msgs = props.messages;
    const last = msgs[msgs.length - 1] as { content?: string } | undefined;
    return `${msgs.length}:${last?.content?.length ?? 0}`;
  },
  () => nextTick(scrollToBottom),
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
  align-items: center;
}

.chat-area > * {
  width: min(800px, 100%);
  /* Never squeeze message blocks when content overflows — scroll instead.
     (overflow:hidden cards have automatic min-size 0 in flex layout and
     would otherwise be compressed to a line.) */
  flex-shrink: 0;
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
  overflow: hidden;
}

.chat-welcome-mark img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.chat-welcome-title {
  font-size: 24px;
  font-weight: 600;
  color: var(--chat-text-primary);
  margin: 0;
}

.chat-welcome-desc {
  font-size: 16px;
  color: var(--chat-text-secondary);
  margin: 0;
}

.chat-loading-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: var(--chat-accent);
  animation: pulse 1.2s ease-in-out infinite;
}

.chat-loading-dot--queued {
  background: #60a5fa;
}

.chat-queue-banner {
  align-self: flex-start;
  margin-top: 8px;
  padding: 6px 12px;
  border-radius: 8px;
  border: 1px solid var(--chat-border, #3a3f4b);
  background: var(--chat-bg-elevated, #23272f);
  color: #60a5fa;
  font-size: 12px;
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
