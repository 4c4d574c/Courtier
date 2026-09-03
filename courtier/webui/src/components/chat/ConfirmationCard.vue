<template>
  <div v-for="item in items" :key="item.confirmationId" class="confirm-card">
    <div class="confirm-head">
      <span class="confirm-tag">待确认</span>
      <code class="confirm-tool">{{ item.toolName }}</code>
    </div>
    <p v-if="item.message" class="confirm-message">{{ item.message }}</p>
    <div class="confirm-actions">
      <button
        class="confirm-btn confirm-btn--primary"
        @click="$emit('resolve', item.confirmationId, 'approve')"
      >
        仅本次执行
      </button>
      <button
        class="confirm-btn confirm-btn--ghost"
        @click="$emit('resolve', item.confirmationId, 'approve_session')"
      >
        本会话内放行
      </button>
      <button
        class="confirm-btn confirm-btn--text"
        @click="$emit('resolve', item.confirmationId, 'deny')"
      >
        拒绝
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import type { PendingConfirmation } from "../../types/agent";

withDefaults(
  defineProps<{
    items?: PendingConfirmation[];
  }>(),
  { items: () => [] },
);

defineEmits<{
  (
    e: "resolve",
    confirmationId: string,
    decision: "approve" | "approve_session" | "deny",
  ): void;
}>();
</script>

<style scoped>
.confirm-card {
  padding: 14px 16px;
  border: 1px solid var(--chat-border);
  border-radius: var(--chat-radius-md);
  background: var(--chat-bg-card);
}

.confirm-head {
  display: flex;
  align-items: center;
  gap: 8px;
}

.confirm-tag {
  font-size: 11px;
  line-height: 1;
  letter-spacing: 0.08em;
  padding: 3px 7px;
  border-radius: 4px;
  color: #9a5b00;
  border: 1px solid rgba(180, 83, 9, 0.35);
  background: rgba(180, 83, 9, 0.06);
}

.confirm-tool {
  font-size: 13px;
  color: var(--chat-text-primary);
}

.confirm-message {
  margin: 8px 0 0;
  font-size: 14px;
  color: var(--chat-text-secondary);
}

.confirm-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px solid var(--chat-border);
}

.confirm-btn {
  font-size: 13px;
  line-height: 1;
  padding: 7px 14px;
  border-radius: var(--chat-radius-sm);
  cursor: pointer;
}

.confirm-btn--primary {
  border: none;
  background: var(--chat-accent);
  color: var(--chat-accent-contrast);
}

.confirm-btn--primary:hover {
  background: var(--chat-accent-hover);
}

.confirm-btn--ghost {
  border: 1px solid var(--chat-border);
  background: transparent;
  color: var(--chat-text-primary);
}

.confirm-btn--ghost:hover {
  border-color: var(--chat-text-tertiary);
}

.confirm-btn--text {
  border: none;
  background: none;
  color: var(--chat-text-secondary);
}

.confirm-btn--text:hover {
  color: var(--chat-accent);
}
</style>
