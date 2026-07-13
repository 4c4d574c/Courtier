<template>
  <div
    :class="[
      'tool-card',
      tool.status,
      { expanded, 'tool-card--skill': tool.skill },
    ]"
    role="button"
    tabindex="0"
    @click="$emit('toggle')"
    @keydown.enter.prevent="$emit('toggle')"
    @keydown.space.prevent="$emit('toggle')"
  >
    <ToolCardCollapsed :tool="tool" @toggle="$emit('toggle')" />
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
