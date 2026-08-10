<template>
  <div
    class="file-card"
    :class="{ 'file-card--previewable': file.url }"
    @click="handleClick"
  >
    <div class="file-card-icon">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
        <polyline points="14 2 14 8 20 8" />
      </svg>
    </div>
    <div class="file-card-info">
      <span class="file-card-name">{{ file.name }}</span>
      <span v-if="!file.url" class="file-card-hint">历史文件，暂不支持预览</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import type { ChatFileItem } from "../../types/chat";

interface Props {
  file: ChatFileItem;
}

const props = defineProps<Props>();
const emit = defineEmits<{
  preview: [file: ChatFileItem];
}>();

function handleClick() {
  if (props.file.url) {
    emit("preview", props.file);
  }
}
</script>

<style scoped>
.file-card {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 6px 10px;
  border: 1px solid var(--chat-border);
  border-radius: var(--chat-radius-md);
  background: var(--chat-bg-card);
  color: var(--chat-text-primary);
  max-width: 70%;
  cursor: default;
  opacity: 0.85;
}

.file-card--previewable {
  cursor: pointer;
  opacity: 1;
}

.file-card--previewable:hover {
  border-color: var(--chat-accent);
}

.file-card-icon {
  width: 24px;
  height: 24px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: var(--chat-radius-sm);
  background: var(--chat-accent-soft);
  color: var(--chat-accent);
  flex-shrink: 0;
}

.file-card-icon svg {
  width: 14px;
  height: 14px;
}

.file-card-info {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.file-card-name {
  font-size: 14px;
  font-weight: 500;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.file-card-hint {
  font-size: 13px;
  color: var(--chat-text-tertiary);
}
</style>
