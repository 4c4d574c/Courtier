<template>
  <div class="status-message" :class="`status-message--${type}`">
    <span class="status-message-icon">{{ type === "error" ? "✕" : "⊘" }}</span>
    <div>
      <div class="status-message-title">{{ title }}</div>
      <div v-if="detail" class="status-message-detail">{{ detail }}</div>
    </div>
  </div>
</template>

<script setup lang="ts">
interface Props {
  type: "error" | "stopped";
  title: string;
  detail?: string;
}
defineProps<Props>();
</script>

<style scoped>
.status-message {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 12px 14px;
  border-radius: var(--chat-radius-md);
  border-left: 3px solid;
  background: var(--chat-bg-card);
  font-size: 14px;
}

.status-message--error {
  border-left-color: #ef4444;
}

.status-message--stopped {
  border-left-color: #f59e0b;
}

.status-message-icon {
  font-weight: 700;
  line-height: 1.4;
}

.status-message--error .status-message-icon {
  color: #ef4444;
}

.status-message--stopped .status-message-icon {
  color: #f59e0b;
}

.status-message-title {
  font-weight: 600;
  color: var(--chat-text-primary);
}

.status-message-detail {
  margin-top: 4px;
  color: var(--chat-text-secondary);
}
</style>
