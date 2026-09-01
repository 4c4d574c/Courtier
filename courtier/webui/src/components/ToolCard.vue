<template>
  <div :class="['tool-card', tool.status, { expanded, 'tool-card--skill': tool.skill }]">
    <!-- Single toggle source: only the header block (title row + summary
         lines) is clickable. The expanded detail stays outside the toggle
         region so text selection and nested controls (e.g. 查看全文) never
         collapse the card. -->
    <div
      class="tool-card-header"
      role="button"
      tabindex="0"
      :aria-expanded="expanded"
      @click="$emit('toggle')"
      @keydown.enter.prevent="$emit('toggle')"
      @keydown.space.prevent="$emit('toggle')"
    >
      <ToolCardCollapsed :tool="tool" :open="expanded" />
    </div>
    <Transition name="expand">
      <ToolCardExpanded v-if="expanded" :tool="tool" />
    </Transition>
  </div>
</template>

<script setup lang="ts">
import type { ToolResult } from "../types/agent";
import ToolCardCollapsed from "./ToolCardCollapsed.vue";
import ToolCardExpanded from "./ToolCardExpanded.vue";

interface Props {
  tool: ToolResult;
  expanded: boolean;
}

defineProps<Props>();
defineEmits<{
  toggle: [];
}>();
</script>
