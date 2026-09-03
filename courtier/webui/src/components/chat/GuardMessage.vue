<template>
  <div class="guard-message" :class="`guard-message--${action}`">
    <span class="guard-message-icon"><AppIcon :name="icon" :size="13" /></span>
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
import AppIcon from "../AppIcon.vue";
import { computed } from "vue";

interface Props {
  layer: string;
  guardName: string;
  action: "allow" | "log" | "block";
  reason?: string;
}

const props = defineProps<Props>();

const icon = computed(() => {
  if (props.action === "block") return "x" as const;
  if (props.action === "log") return "shield" as const;
  return "check" as const;
});

const title = computed(() => {
  if (props.action === "block") return `${props.guardName} 已阻断`;
  if (props.action === "log") return `${props.guardName} 已记录`;
  return `${props.guardName} 已放行`;
});
</script>

<style scoped>
/* Same quiet notice language as ChatStatusMessage: tinted background +
   hairline border in the semantic color, no accent bar. */
.guard-message {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 9px 12px;
  border-radius: var(--chat-radius-sm);
  border: 1px solid;
  font-size: 13px;
  line-height: 1.55;
}

.guard-message--block {
  border-color: color-mix(in srgb, var(--err) 22%, transparent);
  background: color-mix(in srgb, var(--err) 6%, transparent);
}

.guard-message--log {
  border-color: color-mix(in srgb, var(--warn) 24%, transparent);
  background: color-mix(in srgb, var(--warn) 7%, transparent);
}

.guard-message--allow {
  border-color: color-mix(in srgb, var(--ok) 24%, transparent);
  background: color-mix(in srgb, var(--ok) 7%, transparent);
}

.guard-message-icon {
  flex: none;
  margin-top: 2px;
}

.guard-message--block .guard-message-icon {
  color: var(--err);
}

.guard-message--log .guard-message-icon {
  color: var(--warn);
}

.guard-message--allow .guard-message-icon {
  color: var(--ok);
}

.guard-message-title {
  font-weight: 500;
  color: var(--chat-text-primary);
}

.guard-message-layer {
  font-weight: 400;
  color: var(--chat-text-tertiary);
  margin-left: 4px;
}

.guard-message-reason {
  margin-top: 2px;
  color: var(--chat-text-secondary);
}
</style>
