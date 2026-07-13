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
import { ref, onErrorCaptured } from "vue";
const renderError = ref<string | null>(null);
onErrorCaptured((err: unknown) => {
  renderError.value = String(err);
  return false;
});
</script>
