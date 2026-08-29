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
// user input); geometry after the Lucide set (ISC). Sizes are explicit;
// color inherits from currentColor. Paths that need solid ink (menu dots,
// contrast half) set their own fill and opt out of the svg-level stroke.
const PATHS = {
  "chevron-up": '<polyline points="18 15 12 9 6 15"/>',
  "chevron-down": '<polyline points="6 9 12 15 18 15"/>',
  "arrow-left":
    '<line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/>',
  "arrow-right":
    '<line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19"/>',
  check: '<polyline points="20 6 9 17 4 12"/>',
  x: '<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>',
  ban: '<circle cx="12" cy="12" r="9"/><line x1="5.3" y1="5.3" x2="18.7" y2="18.7"/>',
  dots:
    '<circle cx="5" cy="12" r="1.7" fill="currentColor" stroke="none"/>' +
    '<circle cx="12" cy="12" r="1.7" fill="currentColor" stroke="none"/>' +
    '<circle cx="19" cy="12" r="1.7" fill="currentColor" stroke="none"/>',
  gear: lucideGear(),
  sparkle: lucideSparkles(),
  contrast:
    '<circle cx="12" cy="12" r="10"/>' +
    '<path d="M12 2a10 10 0 0 1 0 20z" fill="currentColor" stroke="none"/>',
} as const;

// Modern geometric cog: hexagonal tooth ring around a plain hub — reads
// clean at 16-17px where the old 10-bump feather gear turned into a flower.
function lucideGear(): string {
  return (
    '<path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/>' +
    '<circle cx="12" cy="12" r="3"/>'
  );
}

// One large four-point star with soft concave edges, two small companions —
// balanced at small sizes unlike the previous thin double spark.
function lucideSparkles(): string {
  return (
    '<path d="M9.937 15.5A2 2 0 0 0 8.5 14.063l-6.135-1.582a.5.5 0 0 1 0-.962L8.5 9.936A2 2 0 0 0 9.937 8.5l1.582-6.135a.5.5 0 0 1 .963 0L14.063 8.5A2 2 0 0 0 15.5 9.937l6.135 1.581a.5.5 0 0 1 0 .964L15.5 14.063a2 2 0 0 0-1.437 1.437l-1.582 6.135a.5.5 0 0 1-.963 0z"/>' +
    '<path d="M20 3v4"/><path d="M22 5h-4"/>'
  );
}

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
