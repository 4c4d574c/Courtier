<template>
  <div class="steps-message">
    <StepGroup
      v-for="(group, index) in groups"
      :key="group.step.index"
      :step="group.step"
      :step-thoughts="group.thoughts"
      :is-running="isRunning"
      :is-last-step="index === groups.length - 1"
      :is-expanded="isExpanded"
      :toggle="toggle"
    />
  </div>
</template>

<script setup lang="ts">
import type { StepThoughtGroup } from "../../types/chat";
import StepGroup from "../StepGroup.vue";
import { useToggleSet } from "../../composables/useToggleSet";

interface Props {
  groups: StepThoughtGroup[];
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
