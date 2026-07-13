<template>
  <span class="data-chip">
    {{ label }} <span class="val">{{ value }}</span>
  </span>
</template>

<script setup lang="ts">
interface Props {
  label: string;
  value: string;
}

defineProps<Props>();
</script>
