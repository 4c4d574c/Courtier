<template>
  <header class="chat-header">
    <button
      class="chat-header-menu"
      type="button"
      :title="MESSAGES.CHAT_TOGGLE_SIDEBAR"
      @click="$emit('toggle-sidebar')"
    >
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <line x1="3" y1="6" x2="21" y2="6" />
        <line x1="3" y1="12" x2="21" y2="12" />
        <line x1="3" y1="18" x2="21" y2="18" />
      </svg>
    </button>
    <h1 class="chat-header-title" :title="title">{{ title }}</h1>
    <div class="chat-header-right">
      <span
        v-if="latestFallback"
        class="chat-header-fallback"
        :title="`已切换至备用模型 ${latestFallback.backend}${latestFallback.reason ? '：' + latestFallback.reason : ''}`"
      >
        {{ latestFallback.backend }}
      </span>
      <button
        class="chat-header-icon"
        type="button"
        :title="MESSAGES.CHAT_THEME_TOGGLE"
        @click="$emit('toggle-theme')"
      >
        <svg
          v-if="theme === 'dark'"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2"
        >
          <circle cx="12" cy="12" r="5" />
          <path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42" />
        </svg>
        <svg
          v-else
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2"
        >
          <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
        </svg>
      </button>
    </div>
  </header>
</template>

<script setup lang="ts">
import { computed } from "vue";
import { MESSAGES } from "../../constants/messages";
import { useTheme } from "../../composables/useTheme";
import type { RuntimeEvent } from "../../types/agent";

interface Props {
  title: string;
  modelEvents?: RuntimeEvent[];
}

const props = defineProps<Props>();
defineEmits<{
  "toggle-sidebar": [];
  "toggle-theme": [];
}>();

const { theme } = useTheme();

const latestFallback = computed(() => {
  if (!props.modelEvents?.length) return null;
  for (let i = props.modelEvents.length - 1; i >= 0; i--) {
    if (props.modelEvents[i].type === "model_fallback") {
      return props.modelEvents[i];
    }
  }
  return null;
});
</script>

<style scoped>
.chat-header {
  height: 56px;
  flex-shrink: 0;
  background: var(--chat-bg-card);
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 16px;
  gap: 12px;
}

.chat-header-menu {
  /* Always visible: it toggles the sidebar on desktop (width collapse)
     as well as on mobile (overlay slide-in). */
  display: flex;
  width: 36px;
  height: 36px;
  align-items: center;
  justify-content: center;
  border: none;
  background: transparent;
  color: var(--chat-text-secondary);
  border-radius: var(--chat-radius-md);
  cursor: pointer;
}

.chat-header-menu:hover {
  background: var(--chat-bg-hover);
  color: var(--chat-text-primary);
}

.chat-header-menu svg {
  width: 20px;
  height: 20px;
}

.chat-header-title {
  flex: 1;
  font-size: 18px;
  font-weight: 600;
  color: var(--chat-text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  margin: 0;
}

.chat-header-right {
  display: flex;
  align-items: center;
  gap: 12px;
}

.chat-header-fallback {
  font-size: 13px;
  color: #f59e0b;
  padding: 4px 8px;
  border: 1px solid #f59e0b;
  border-radius: 999px;
  background: rgba(245, 158, 11, 0.08);
}

.chat-header-icon {
  width: 36px;
  height: 36px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: none;
  background: transparent;
  color: var(--chat-text-secondary);
  border-radius: var(--chat-radius-md);
  cursor: pointer;
}

.chat-header-icon:hover {
  background: var(--chat-bg-hover);
  color: var(--chat-text-primary);
}

.chat-header-icon svg {
  width: 18px;
  height: 18px;
}
</style>
