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
      <span v-if="modelName" class="chat-header-model">{{ modelName }}</span>
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
        title="调试面板"
        @click="$emit('toggle-debug')"
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M12 2a4 4 0 0 1 4 4v1h3a1 1 0 0 1 1 1v2a1 1 0 0 1-1 1h-1v4h1a1 1 0 0 1 1 1v2a1 1 0 0 1-1 1h-3a4 4 0 0 1-8 0H5a1 1 0 0 1-1-1v-2a1 1 0 0 1 1-1h1v-4H5a1 1 0 0 1-1-1V8a1 1 0 0 1 1-1h3a4 4 0 0 1 4-4z" />
        </svg>
      </button>
      <button
        class="chat-header-icon"
        type="button"
        :title="MESSAGES.CHAT_THEME_TOGGLE"
        @click="$emit('toggle-theme')"
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <circle cx="12" cy="12" r="5" />
          <path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42" />
        </svg>
      </button>
      <div class="chat-header-user" @click="menuOpen = !menuOpen">
        <span class="chat-header-user-name">{{ username || "用户" }}</span>
        <span class="chat-header-user-arrow">▾</span>
        <div v-if="menuOpen" class="chat-header-dropdown">
          <router-link to="/profile" class="chat-header-dropdown-item" @click.stop>
            {{ MESSAGES.CHAT_SETTINGS }}
          </router-link>
          <template v-if="isAdmin">
            <router-link to="/admin/users" class="chat-header-dropdown-item" @click.stop>
              {{ MESSAGES.CHAT_USER_MANAGE }}
            </router-link>
            <router-link to="/admin/approvals" class="chat-header-dropdown-item" @click.stop>
              {{ MESSAGES.CHAT_APPROVALS }}
            </router-link>
          </template>
          <button class="chat-header-dropdown-item" type="button" @click.stop="logout">
            {{ MESSAGES.CHAT_LOGOUT }}
          </button>
        </div>
      </div>
    </div>
  </header>
</template>

<script setup lang="ts">
import { ref, computed } from "vue";
import { MESSAGES } from "../../constants/messages";
import type { RuntimeEvent } from "../../types/agent";

interface Props {
  title: string;
  modelName?: string;
  username?: string;
  userRole?: string;
  modelEvents?: RuntimeEvent[];
}

const props = defineProps<Props>();
const emit = defineEmits<{
  "toggle-sidebar": [];
  "toggle-debug": [];
  "toggle-theme": [];
  logout: [];
}>();

const menuOpen = ref(false);
const isAdmin = computed(() => props.userRole === "admin");

const latestFallback = computed(() => {
  if (!props.modelEvents?.length) return null;
  for (let i = props.modelEvents.length - 1; i >= 0; i--) {
    if (props.modelEvents[i].type === "model_fallback") {
      return props.modelEvents[i];
    }
  }
  return null;
});

function logout() {
  menuOpen.value = false;
  emit("logout");
}
</script>

<style scoped>
.chat-header {
  height: 56px;
  flex-shrink: 0;
  background: var(--chat-bg-card);
  border-bottom: 1px solid var(--chat-border);
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 16px;
  gap: 12px;
}

.chat-header-menu {
  display: none;
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
  font-size: 16px;
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

.chat-header-model {
  font-size: 12px;
  color: var(--chat-text-tertiary);
  padding: 4px 8px;
  border: 1px solid var(--chat-border);
  border-radius: 999px;
}

.chat-header-fallback {
  font-size: 12px;
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

.chat-header-user {
  position: relative;
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 6px 10px;
  border-radius: var(--chat-radius-md);
  cursor: pointer;
  color: var(--chat-text-secondary);
}

.chat-header-user:hover {
  background: var(--chat-bg-hover);
}

.chat-header-user-name {
  font-size: 14px;
  color: var(--chat-text-primary);
}

.chat-header-user-arrow {
  font-size: 10px;
}

.chat-header-dropdown {
  position: absolute;
  top: 100%;
  right: 0;
  margin-top: 6px;
  min-width: 140px;
  background: var(--chat-bg-card);
  border: 1px solid var(--chat-border);
  border-radius: var(--chat-radius-md);
  box-shadow: var(--chat-shadow);
  overflow: hidden;
  z-index: 100;
}

.chat-header-dropdown-item {
  display: block;
  padding: 10px 14px;
  font-size: 14px;
  color: var(--chat-text-primary);
  text-decoration: none;
  text-align: left;
  background: transparent;
  border: none;
  width: 100%;
  cursor: pointer;
}

.chat-header-dropdown-item:hover {
  background: var(--chat-bg-hover);
}

@media (max-width: 768px) {
  .chat-header-menu {
    display: flex;
  }
  .chat-header-model {
    display: none;
  }
}
</style>
