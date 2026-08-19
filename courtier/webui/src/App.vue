<template>
  <div v-if="renderError" class="render-error-fallback">
    <div class="render-error-icon">!</div>
    <h2>界面渲染错误</h2>
    <p>{{ renderError }}</p>
    <button @click="renderError = null">重试</button>
  </div>
  <router-view v-else />
</template>

<script setup lang="ts">
import { ref, onErrorCaptured, watch } from "vue";
import { useAuth } from "./composables/useAuth";
import { useRunEvents } from "./composables/useRunEvents";

const renderError = ref<string | null>(null);
onErrorCaptured((err: unknown) => {
  renderError.value = String(err);
  return false;
});

// Global run-events channel: lives for the whole logged-in app lifetime
// (sidebar badges / completion toasts), independent of the open view.
const { isLoggedIn } = useAuth();
const runEvents = useRunEvents();
watch(
  isLoggedIn,
  (loggedIn) => {
    if (loggedIn) runEvents.start();
    else runEvents.stop();
  },
  { immediate: true },
);
</script>
