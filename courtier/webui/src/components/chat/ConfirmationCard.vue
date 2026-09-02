<template>
  <div v-for="item in items" :key="item.confirmationId" class="confirm-card">
    <span class="confirm-card-icon"><AppIcon name="shield" :size="15" /></span>
    <div class="confirm-card-body">
      <div class="confirm-card-title">
        工具 <code>{{ item.toolName }}</code> 请求执行，等待你的确认
      </div>
      <div v-if="item.message" class="confirm-card-message">{{ item.message }}</div>
      <div class="confirm-card-actions">
        <button class="confirm-btn confirm-btn--once" @click="$emit('resolve', item.confirmationId, 'approve')">
          仅本次执行
        </button>
        <button class="confirm-btn confirm-btn--session" @click="$emit('resolve', item.confirmationId, 'approve_session')">
          本会话内放行
        </button>
        <button class="confirm-btn confirm-btn--deny" @click="$emit('resolve', item.confirmationId, 'deny')">
          拒绝
        </button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import type { PendingConfirmation } from "../../types/agent";
import AppIcon from "../AppIcon.vue";

defineProps<{
  items: PendingConfirmation[];
}>();

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
  display: flex;
  align-items: flex-start;
  gap: 10px;
  margin: 8px 0;
  padding: 12px 14px;
  border-radius: var(--chat-radius-md);
  border-left: 3px solid #f59e0b;
  background: var(--chat-bg-card);
  font-size: 15px;
}

.confirm-card-icon {
  color: #f59e0b;
  margin-top: 2px;
}

.confirm-card-title code {
  font-size: 13px;
  padding: 1px 5px;
  border-radius: 4px;
  background: var(--chat-bg-input, rgba(127, 127, 127, 0.15));
}

.confirm-card-message {
  margin-top: 4px;
  color: var(--chat-text-secondary, inherit);
  opacity: 0.85;
  font-size: 14px;
}

.confirm-card-actions {
  display: flex;
  gap: 8px;
  margin-top: 10px;
  flex-wrap: wrap;
}

.confirm-btn {
  border: 1px solid var(--chat-border, rgba(127, 127, 127, 0.3));
  background: transparent;
  color: inherit;
  border-radius: 8px;
  padding: 5px 12px;
  font-size: 13px;
  cursor: pointer;
}

.confirm-btn--once {
  border-color: #3b82f6;
  color: #3b82f6;
}

.confirm-btn--session {
  border-color: #22c55e;
  color: #22c55e;
}

.confirm-btn--deny {
  border-color: #ef4444;
  color: #ef4444;
}

.confirm-btn:hover {
  filter: brightness(1.15);
}
</style>
