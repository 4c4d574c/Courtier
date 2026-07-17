<template>
  <div class="guard-message" :class="`guard-message--${action}`">
    <span class="guard-message-icon">{{ icon }}</span>
    <div>
      <div class="guard-message-title">
        {{ title }}
        <span v-if="layer" class="guard-message-layer">({{ layer }})</span>
      </div>
      <div v-if="reason" class="guard-message-reason">{{ reason }}</div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from "vue";

interface Props {
  layer: string;
  guardName: string;
  action: "allow" | "log" | "block";
  reason?: string;
}

const props = defineProps<Props>();

const icon = computed(() => {
  if (props.action === "block") return "✕";
  if (props.action === "log") return "⊘";
  return "✓";
});

const title = computed(() => {
  if (props.action === "block") return `${props.guardName} 已阻断`;
  if (props.action === "log") return `${props.guardName} 已记录`;
  return `${props.guardName} 已放行`;
});
</script>

<style scoped>
.guard-message {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 12px 14px;
  border-radius: var(--chat-radius-md);
  border-left: 3px solid;
  background: var(--chat-bg-card);
  font-size: 14px;
}

.guard-message--block {
  border-left-color: #ef4444;
}

.guard-message--log {
  border-left-color: #f59e0b;
}

.guard-message--allow {
  border-left-color: #22c55e;
}

.guard-message-icon {
  font-weight: 700;
  line-height: 1.4;
}

.guard-message--block .guard-message-icon {
  color: #ef4444;
}

.guard-message--log .guard-message-icon {
  color: #f59e0b;
}

.guard-message--allow .guard-message-icon {
  color: #22c55e;
}

.guard-message-title {
  font-weight: 600;
  color: var(--chat-text-primary);
}

.guard-message-layer {
  font-weight: 400;
  color: var(--chat-text-tertiary);
  margin-left: 4px;
}

.guard-message-reason {
  margin-top: 4px;
  color: var(--chat-text-secondary);
}
</style>
