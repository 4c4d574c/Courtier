<template>
  <AppIcon
    class="disclosure-chevron"
    :class="{ 'disclosure-chevron--open': open }"
    name="chevron-right"
    :size="size"
  />
</template>

<script setup lang="ts">
// The app-wide disclosure indicator: points right while collapsed, rotates
// down when open. Pair with useDisclosure (single block) or useToggleSet
// (keyed sets) for the state.
import AppIcon from "./AppIcon.vue";

withDefaults(
  defineProps<{
    open?: boolean;
    size?: number;
  }>(),
  { open: false, size: 14 },
);
</script>

<style scoped>
.disclosure-chevron {
  display: block;
  /* No transform transition on purpose: when the document rendering timeline
     is throttled (backgrounded pane), a CSSTransition freezes at its first
     frame and locks the computed transform to the pre-rotation value — the
     arrow would point the wrong way until the next real frame. An instant
     flip is deterministic everywhere. */
}

.disclosure-chevron--open {
  transform: rotate(90deg);
}
</style>
