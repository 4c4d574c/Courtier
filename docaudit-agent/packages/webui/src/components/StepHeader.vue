<template>
  <div class="step-header">
    <span v-if="skill" class="step-label">{{ skill }}</span>
    <span v-else class="step-label">{{ label }}</span>
  </div>
</template>

<script setup lang="ts">
interface Props {
  numeral: string;
  label: string;
  skill: string;
}

defineProps<Props>();
</script>
