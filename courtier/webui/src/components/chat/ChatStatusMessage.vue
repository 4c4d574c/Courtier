<template>
  <div class="status-message" :class="`status-message--${type}`">
    <span class="status-message-icon"><AppIcon :name="type === 'error' ? 'circle-alert' : 'ban'" :size="14" /></span>
    <div>
      <div class="status-message-title">{{ title }}</div>
      <div v-if="detail" class="status-message-detail">{{ detail }}</div>
    </div>
  </div>
</template>

<script setup lang="ts">
import AppIcon from "../AppIcon.vue";
interface Props {
  type: "error" | "stopped";
  title: string;
  detail?: string;
}
defineProps<Props>();
</script>

<style scoped>
/* Quiet inline notice: soft semantic tint + hairline border, no accent bar.
   Deliberately smaller than message text so it reads as system status. */
.status-message {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 9px 12px;
  border-radius: var(--chat-radius-sm);
  border: 1px solid;
  font-size: 13px;
  line-height: 1.55;
}

.status-message--error {
  border-color: color-mix(in srgb, var(--err) 22%, transparent);
  background: color-mix(in srgb, var(--err) 6%, transparent);
}

.status-message--stopped {
  border-color: color-mix(in srgb, var(--warn) 24%, transparent);
  background: color-mix(in srgb, var(--warn) 7%, transparent);
}

.status-message-icon {
  flex: none;
  margin-top: 2px;
}

.status-message--error .status-message-icon {
  color: var(--err);
}

.status-message--stopped .status-message-icon {
  color: var(--warn);
}

.status-message-title {
  font-weight: 500;
  color: var(--chat-text-primary);
}

.status-message-detail {
  margin-top: 2px;
  color: var(--chat-text-secondary);
}
</style>
