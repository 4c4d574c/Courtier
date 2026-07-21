<template>
  <div class="steps-message">
    <StepGroup
      v-for="(step, index) in steps"
      :key="step.index"
      :step="step"
      :step-thoughts="[]"
      :is-running="isRunning"
      :is-last-step="index === steps.length - 1"
      :is-expanded="isExpanded"
      :toggle="toggle"
    />
  </div>
</template>

<script setup lang="ts">
import type { Step } from "../../types/agent";
import StepGroup from "../StepGroup.vue";
import { useToggleSet } from "../../composables/useToggleSet";

interface Props {
  steps: Step[];
  isRunning: boolean;
}

defineProps<Props>();

// Tool cards start collapsed; expansion state is local to this turn's block.
const { toggle, isExpanded } = useToggleSet();
</script>

<style scoped>
.steps-message {
  display: flex;
  flex-direction: column;
  gap: 2px;
  position: relative;
  border: 1px solid var(--chat-border);
  border-radius: var(--chat-radius-lg);
  background: var(--chat-bg-card);
  padding: 8px 12px 8px 14px;
}

/* Vertical timeline line behind the row icons (Kimi-style process list). */
.steps-message::before {
  content: "";
  position: absolute;
  left: 22px;
  top: 18px;
  bottom: 18px;
  width: 1px;
  background: var(--chat-border);
}
</style>
