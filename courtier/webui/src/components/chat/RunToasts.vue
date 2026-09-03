<template>
  <div class="run-toasts" aria-live="polite">
    <button
      v-for="toast in toasts"
      :key="toast.id"
      class="run-toast"
      :class="toast.kind"
      type="button"
      @click="$emit('select', toast.sessionId); dismiss(toast.id)"
    >
      <span class="run-toast-dot"></span>
      <span class="run-toast-body">
        <span class="run-toast-title">{{ toast.title }}</span>
        <span v-if="toast.detail" class="run-toast-detail">{{ toast.detail }}</span>
      </span>
      <span class="run-toast-close" @click.stop="dismiss(toast.id)">×</span>
    </button>
  </div>
</template>

<script setup lang="ts">
import { ref } from "vue";

export interface RunToast {
  id: number;
  sessionId: string;
  kind: "completed" | "error" | "stopped";
  title: string;
  detail?: string;
}

const toasts = ref<RunToast[]>([]);
let nextId = 0;
const timers = new Map<number, ReturnType<typeof setTimeout>>();

defineEmits<{ select: [sessionId: string] }>();

function push(toast: Omit<RunToast, "id">) {
  const id = ++nextId;
  toasts.value = [...toasts.value, { ...toast, id }];
  timers.set(
    id,
    setTimeout(() => dismiss(id), 8000),
  );
}

function dismiss(id: number) {
  const timer = timers.get(id);
  if (timer) clearTimeout(timer);
  timers.delete(id);
  toasts.value = toasts.value.filter((t) => t.id !== id);
}

defineExpose({ push, dismiss });
</script>

<style scoped>
.run-toasts {
  position: fixed;
  right: 16px;
  bottom: 16px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  z-index: 1000;
  max-width: 340px;
}

.run-toast {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 10px 12px;
  border: 1px solid var(--chat-border);
  border-radius: var(--chat-radius-sm);
  background: var(--chat-bg-card);
  color: var(--chat-text-primary);
  text-align: left;
  cursor: pointer;
  box-shadow: var(--chat-shadow);
  animation: run-toast-in 0.18s ease-out;
}

@keyframes run-toast-in {
  from {
    opacity: 0;
    transform: translateY(6px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

.run-toast-dot {
  width: 8px;
  height: 8px;
  margin-top: 5px;
  border-radius: 50%;
  flex-shrink: 0;
}

.run-toast.completed .run-toast-dot {
  background: var(--ok);
}

.run-toast.error .run-toast-dot {
  background: var(--err);
}

.run-toast.stopped .run-toast-dot {
  background: var(--chat-text-tertiary);
}

.run-toast-body {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.run-toast-title {
  font-size: 13px;
  font-weight: 500;
}

.run-toast-detail {
  font-size: 12px;
  color: var(--chat-text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}

.run-toast-close {
  margin-left: 6px;
  color: var(--chat-text-tertiary);
  font-size: 16px;
  line-height: 1;
}
</style>
