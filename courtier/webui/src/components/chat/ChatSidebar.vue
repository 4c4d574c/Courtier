<template>
  <aside class="chat-sidebar" :class="{ 'chat-sidebar--open': isOpen }">
    <div class="chat-sidebar-header">
      <div class="chat-sidebar-logo">
        <span class="chat-sidebar-logo-mark">审</span>
        <span class="chat-sidebar-logo-text">SDTAgent</span>
      </div>
      <button
        class="chat-sidebar-new"
        type="button"
        @click="$emit('new-session')"
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M12 5v14M5 12h14" />
        </svg>
        {{ MESSAGES.CHAT_NEW_SESSION }}
      </button>
    </div>

    <div class="chat-sidebar-section">
      <h3 class="chat-sidebar-section-title">{{ MESSAGES.CHAT_HISTORY_TITLE }}</h3>
      <div v-if="loading" class="chat-sidebar-loading">{{ MESSAGES.LOADING }}</div>
      <div v-else-if="sessions.length === 0" class="chat-sidebar-empty">
        {{ MESSAGES.CHAT_NO_HISTORY }}
      </div>
      <ul v-else class="chat-sidebar-list">
        <li
          v-for="session in sessions"
          :key="session.id"
          class="chat-sidebar-item"
          @click="$emit('select', session.id)"
        >
          <span class="chat-sidebar-item-task">{{ session.task }}</span>
          <span class="chat-sidebar-item-meta">
            <span
              class="chat-sidebar-item-dot"
              :class="session.status"
            ></span>
            {{ formatDateTime(session.createdAt) }}
          </span>
          <button
            class="chat-sidebar-item-delete"
            type="button"
            @click.stop="$emit('delete', session.id)"
            :aria-label="MESSAGES.DELETE_CONFIRM"
          >
            ×
          </button>
        </li>
      </ul>
    </div>

    <div v-if="isAdmin" class="chat-sidebar-section">
      <h3 class="chat-sidebar-section-title">管理</h3>
      <nav class="chat-sidebar-nav">
        <router-link to="/admin/users" class="chat-sidebar-nav-item">
          {{ MESSAGES.CHAT_USER_MANAGE }}
        </router-link>
        <router-link to="/admin/approvals" class="chat-sidebar-nav-item">
          {{ MESSAGES.CHAT_APPROVALS }}
        </router-link>
      </nav>
    </div>

    <div class="chat-sidebar-footer">
      <router-link to="/profile" class="chat-sidebar-footer-item">
        {{ MESSAGES.CHAT_SETTINGS }}
      </router-link>
      <button class="chat-sidebar-footer-item" type="button" @click="$emit('logout')">
        {{ MESSAGES.CHAT_LOGOUT }}
      </button>
    </div>
  </aside>
</template>

<script setup lang="ts">
import { computed } from "vue";
import type { SessionSummary } from "../../types/agent";
import { MESSAGES } from "../../constants/messages";
import { formatDateTime } from "../../utils/date";

interface Props {
  sessions: SessionSummary[];
  loading: boolean;
  isOpen: boolean;
  userRole?: string;
}

const props = defineProps<Props>();
defineEmits<{
  "new-session": [];
  select: [id: string];
  delete: [id: string];
  logout: [];
}>();

const isAdmin = computed(() => props.userRole === "admin");
</script>

<style scoped>
.chat-sidebar {
  width: var(--chat-sidebar-width);
  flex-shrink: 0;
  background: var(--chat-bg-card);
  border-right: 1px solid var(--chat-border);
  display: flex;
  flex-direction: column;
  height: 100%;
  transition: transform 0.25s ease;
}

.chat-sidebar-header {
  padding: 16px;
  border-bottom: 1px solid var(--chat-border);
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.chat-sidebar-logo {
  display: flex;
  align-items: center;
  gap: 10px;
}

.chat-sidebar-logo-mark {
  width: 32px;
  height: 32px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: 2px solid var(--chat-accent);
  border-radius: var(--chat-radius-sm);
  color: var(--chat-accent);
  font-weight: 700;
  font-size: 16px;
}

.chat-sidebar-logo-text {
  font-size: 18px;
  font-weight: 600;
  color: var(--chat-text-primary);
}

.chat-sidebar-new {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 8px 12px;
  border-radius: var(--chat-radius-md);
  border: 1px solid var(--chat-border);
  background: var(--chat-bg-card);
  color: var(--chat-text-primary);
  cursor: pointer;
  transition:
    background 0.15s,
    border-color 0.15s;
}

.chat-sidebar-new:hover {
  background: var(--chat-bg-hover);
  border-color: var(--chat-accent);
}

.chat-sidebar-new svg {
  width: 16px;
  height: 16px;
}

.chat-sidebar-section {
  padding: 12px 12px 4px;
}

.chat-sidebar-section-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--chat-text-tertiary);
  text-transform: uppercase;
  letter-spacing: 0.06em;
  margin-bottom: 8px;
  padding: 0 4px;
}

.chat-sidebar-loading,
.chat-sidebar-empty {
  padding: 12px 4px;
  font-size: 14px;
  color: var(--chat-text-secondary);
}

.chat-sidebar-list {
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.chat-sidebar-item {
  position: relative;
  padding: 10px;
  border-radius: var(--chat-radius-md);
  cursor: pointer;
  transition: background 0.15s;
}

.chat-sidebar-item:hover {
  background: var(--chat-bg-hover);
}

.chat-sidebar-item-task {
  display: block;
  font-size: 14px;
  color: var(--chat-text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.chat-sidebar-item-meta {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--chat-text-tertiary);
  margin-top: 4px;
}

.chat-sidebar-item-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
}

.chat-sidebar-item-dot.completed {
  background: #22c55e;
}
.chat-sidebar-item-dot.running {
  background: #f59e0b;
}
.chat-sidebar-item-dot.error {
  background: #ef4444;
}

.chat-sidebar-item-delete {
  position: absolute;
  top: 6px;
  right: 6px;
  width: 22px;
  height: 22px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: none;
  background: transparent;
  color: var(--chat-text-tertiary);
  opacity: 0;
  cursor: pointer;
  border-radius: var(--chat-radius-sm);
  transition: opacity 0.15s;
}

.chat-sidebar-item:hover .chat-sidebar-item-delete {
  opacity: 1;
}

.chat-sidebar-item-delete:hover {
  background: rgba(239, 68, 68, 0.1);
  color: #ef4444;
}

.chat-sidebar-nav {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.chat-sidebar-nav-item {
  padding: 8px 10px;
  border-radius: var(--chat-radius-md);
  font-size: 14px;
  color: var(--chat-text-primary);
  text-decoration: none;
  transition: background 0.15s;
}

.chat-sidebar-nav-item:hover {
  background: var(--chat-bg-hover);
}

.chat-sidebar-footer {
  margin-top: auto;
  border-top: 1px solid var(--chat-border);
  padding: 8px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.chat-sidebar-footer-item {
  padding: 8px 10px;
  border-radius: var(--chat-radius-md);
  font-size: 14px;
  color: var(--chat-text-primary);
  text-decoration: none;
  text-align: left;
  background: transparent;
  border: none;
  cursor: pointer;
  transition: background 0.15s;
}

.chat-sidebar-footer-item:hover {
  background: var(--chat-bg-hover);
}

@media (max-width: 768px) {
  .chat-sidebar {
    position: fixed;
    left: 0;
    top: 0;
    bottom: 0;
    z-index: 100;
    transform: translateX(-100%);
  }
  .chat-sidebar--open {
    transform: translateX(0);
  }
}
</style>
