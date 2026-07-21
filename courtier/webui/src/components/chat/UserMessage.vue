<template>
  <div class="user-message">
    <div class="user-message-bubble">
      <div class="user-message-text">{{ content }}</div>
    </div>
  </div>
</template>

<script setup lang="ts">
interface Props {
  content: string;
}
defineProps<Props>();
</script>

<style scoped>
.user-message {
  display: flex;
  justify-content: flex-end;
}

.user-message-bubble {
  max-width: 80%;
  background: var(--chat-bg-hover);
  color: var(--chat-text-primary);
  border-radius: var(--chat-radius-lg);
  padding: 12px 16px;
  font-size: 15px;
  line-height: 1.6;
}
</style>
