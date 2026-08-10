<template>
  <Teleport to="body">
    <Transition name="drawer-fade">
      <div v-if="isOpen" class="file-drawer-backdrop" @click="$emit('close')">
        <Transition name="drawer-slide">
          <div
            v-if="isOpen"
            class="file-drawer"
            role="dialog"
            aria-modal="true"
            :aria-labelledby="titleId"
            @click.stop
          >
            <div class="file-drawer-header">
              <h3 :id="titleId" class="file-drawer-title" :title="file?.name">{{ file?.name }}</h3>
              <div class="file-drawer-actions">
                <a
                  v-if="file?.url"
                  class="file-drawer-action"
                  :href="file.url"
                  :download="file.name"
                >
                  {{ MESSAGES.CHAT_PREVIEW_DOWNLOAD }}
                </a>
                <button
                  class="file-drawer-close"
                  type="button"
                  :aria-label="MESSAGES.CHAT_PREVIEW_CLOSE"
                  @click="$emit('close')"
                >
                  ×
                </button>
              </div>
            </div>

            <div class="file-drawer-body">
              <img
                v-if="file && isImage(file.mimeType)"
                :src="file.url"
                :alt="file.name"
                class="file-drawer-image"
              />
              <iframe
                v-else-if="file && isPdf(file.mimeType)"
                :src="file.url"
                class="file-drawer-frame"
                :title="file.name"
              ></iframe>
              <div v-else class="file-drawer-unsupported">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="48" height="48">
                  <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
                  <polyline points="14 2 14 8 20 8" />
                </svg>
                <p>{{ MESSAGES.CHAT_PREVIEW_UNSUPPORTED }}</p>
                <a v-if="file?.url" class="file-drawer-download-link" :href="file.url" :download="file.name">
                  {{ MESSAGES.CHAT_PREVIEW_DOWNLOAD }}
                </a>
              </div>
            </div>
          </div>
        </Transition>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, watch } from "vue";
import type { ChatFileItem } from "../../types/chat";
import { MESSAGES } from "../../constants/messages";

const titleId = "file-drawer-title";

interface Props {
  isOpen: boolean;
  file: ChatFileItem | null;
}

const props = defineProps<Props>();
const emit = defineEmits<{ close: [] }>();

function isImage(mime: string): boolean {
  return mime.startsWith("image/");
}

function isPdf(mime: string): boolean {
  return mime === "application/pdf";
}

function onKeydown(event: KeyboardEvent) {
  if (event.key === "Escape" && props.isOpen) {
    emit("close");
  }
}

onMounted(() => window.addEventListener("keydown", onKeydown));
onUnmounted(() => {
  window.removeEventListener("keydown", onKeydown);
  document.body.style.overflow = "";
});

watch(
  () => props.isOpen,
  (open) => {
    document.body.style.overflow = open ? "hidden" : "";
  },
  { immediate: true },
);
</script>

<style scoped>
.file-drawer-backdrop {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.4);
  z-index: 200;
  display: flex;
  justify-content: flex-end;
}

.file-drawer {
  width: var(--chat-drawer-width);
  max-width: 100%;
  height: 100%;
  background: var(--chat-bg-card);
  display: flex;
  flex-direction: column;
  box-shadow: -4px 0 24px rgba(0, 0, 0, 0.12);
}

.file-drawer-header {
  flex-shrink: 0;
  height: 56px;
  padding: 0 16px;
  border-bottom: 1px solid var(--chat-border);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.file-drawer-title {
  flex: 1;
  font-size: 16px;
  font-weight: 600;
  color: var(--chat-text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  margin: 0;
}

.file-drawer-actions {
  display: flex;
  align-items: center;
  gap: 12px;
}

.file-drawer-action {
  font-size: 15px;
  color: var(--chat-accent);
  text-decoration: none;
}

.file-drawer-action:hover {
  text-decoration: underline;
}

.file-drawer-close {
  width: 32px;
  height: 32px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: none;
  background: transparent;
  color: var(--chat-text-secondary);
  font-size: 26px;
  line-height: 1;
  cursor: pointer;
  border-radius: var(--chat-radius-md);
}

.file-drawer-close:hover {
  background: var(--chat-bg-hover);
  color: var(--chat-text-primary);
}

.file-drawer-body {
  flex: 1;
  overflow: auto;
  padding: 16px;
  display: flex;
  align-items: center;
  justify-content: center;
}

.file-drawer-image {
  max-width: 100%;
  max-height: 100%;
  object-fit: contain;
  border-radius: var(--chat-radius-md);
}

.file-drawer-frame {
  width: 100%;
  height: 100%;
  border: none;
  border-radius: var(--chat-radius-md);
}

.file-drawer-unsupported {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
  text-align: center;
  color: var(--chat-text-secondary);
}

.file-drawer-download-link {
  padding: 8px 16px;
  border-radius: var(--chat-radius-md);
  background: var(--chat-accent);
  color: #fff;
  text-decoration: none;
  font-size: 15px;
}

.drawer-fade-enter-active,
.drawer-fade-leave-active {
  transition: opacity 0.25s ease;
}

.drawer-fade-enter-from,
.drawer-fade-leave-to {
  opacity: 0;
}

.drawer-slide-enter-active,
.drawer-slide-leave-active {
  transition: transform 0.25s ease;
}

.drawer-slide-enter-from,
.drawer-slide-leave-to {
  transform: translateX(100%);
}

@media (max-width: 768px) {
  .file-drawer {
    width: 100%;
  }
}
</style>
