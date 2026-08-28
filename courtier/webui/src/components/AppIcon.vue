<template>
  <svg
    class="app-icon"
    :width="size"
    :height="size"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="2"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
    v-html="PATHS[name]"
  ></svg>
</template>

<script setup lang="ts">
// Shared UI icon set — inline SVG so icon rendering never depends on
// platform emoji/text fonts. Static single-source markup (never built from
// user input). Sizes are explicit; color inherits from currentColor.
const PATHS = {
  "chevron-up": '<polyline points="18 15 12 9 6 15"/>',
  "chevron-down": '<polyline points="6 9 12 15 18 15"/>',
  "arrow-left": '<line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/>',
  "arrow-right": '<line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19"/>',
  check: '<polyline points="20 6 9 17 4 12"/>',
  x: '<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>',
  ban: '<circle cx="12" cy="12" r="9"/><line x1="5.3" y1="5.3" x2="18.7" y2="18.7"/>',
  dots: '<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>',
} as const;

defineProps<{
  name: keyof typeof PATHS;
  size?: number;
}>();
</script>

<style scoped>
.app-icon {
  display: inline-block;
  vertical-align: -0.15em;
  flex-shrink: 0;
}
</style>
