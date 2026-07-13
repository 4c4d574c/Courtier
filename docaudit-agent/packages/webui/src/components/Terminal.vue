<template>
  <div class="page">
    <div class="top-nav">
      <div class="top-nav-left">
        <div class="seal">
          <span class="seal-text">审</span>
        </div>
        <div class="top-nav-brand">
          <span class="top-nav-title">SDTAgent</span>
          <span v-if="modelName" class="top-nav-model">/{{ modelName }}</span>
        </div>
      </div>
      <div class="top-nav-right">
        <div class="top-nav-actions">
          <span
            class="top-nav-action"
            @click="$emit('new-session')"
            :title="MESSAGES.NEW_SESSION"
          >
            <svg
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
            >
              <path d="M12 5v14M5 12h14" />
            </svg>
            {{ MESSAGES.NEW_SESSION }}
          </span>
          <span
            class="top-nav-action"
            @click="$emit('export')"
            :title="MESSAGES.EXPORT"
          >
            <svg
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
            >
              <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
              <polyline points="7 10 12 15 17 10" />
              <line x1="12" y1="15" x2="12" y2="3" />
            </svg>
            {{ MESSAGES.EXPORT }}
          </span>
        </div>
        <div v-if="username" class="user-menu" @click="menuOpen = !menuOpen">
          <span class="user-name">{{ username }}</span>
          <span class="user-arrow">▾</span>
          <div v-if="menuOpen" class="user-dropdown">
            <router-link to="/profile" class="dropdown-item" @click.stop
              >个人设置</router-link
            >
            <template v-if="userRole === 'admin'">
              <router-link to="/admin/users" class="dropdown-item" @click.stop
                >用户管理</router-link
              >
              <router-link
                to="/admin/approvals"
                class="dropdown-item"
                @click.stop
                >注册审批</router-link
              >
            </template>
            <div class="dropdown-divider"></div>
            <button
              class="dropdown-item logout-item"
              @click.stop="handleLogout"
            >
              退出登录
            </button>
          </div>
        </div>
      </div>
    </div>
    <div class="main-content">
      <HistoryPanel
        :open="historyOpen"
        :sessions="historySessions"
        :loading="historyLoading"
        @toggle="$emit('history-toggle')"
        @select="(id: string) => $emit('history-select', id)"
        @delete="(id: string) => $emit('history-delete', id)"
      />
      <div class="main-column">
        <MainPanel
          :turns="turns"
          :turn-version="turnVersion"
          :thoughts="thoughts"
          :is-expanded="isExpanded"
          :toggle="toggle"
          :stats="stats"
          :step-count="stepCount"
          :total-tools="totalTools"
          :done-tools="doneTools"
          :issue-count="issueCount"
          :error-message="errorMessage"
          :stop-reason="stopReason"
          :is-running="isRunning"
          @quick-task="(task: string) => $emit('quick-task', task)"
        />
        <InputPanel
          :uploading="uploading"
          :error="uploadError"
          :is-running="isRunning"
          @submit="(task: string, file?: File) => $emit('submit', task, file)"
          @stop="$emit('stop')"
        />
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from "vue";
import type { Thought, Turn, SessionSummary } from "../types/agent";
import { MESSAGES } from "../constants/messages";
import InputPanel from "./InputPanel.vue";
import MainPanel from "./MainPanel.vue";
import HistoryPanel from "./HistoryPanel.vue";

interface Props {
  stats: { tokensIn: number; tokensOut: number };
  turns: Turn[];
  turnVersion: number;
  thoughts: Thought[];
  isRunning: boolean;
  stepCount: number;
  totalTools: number;
  doneTools: number;
  issueCount: number;
  isExpanded: (name: string) => boolean;
  toggle: (name: string) => void;
  uploading: boolean;
  uploadError: string;
  modelName: string;
  historyOpen: boolean;
  historySessions: SessionSummary[];
  historyLoading: boolean;
  errorMessage?: string;
  stopReason?: string;
  username?: string;
  userRole?: string;
}

defineProps<Props>();
const emit = defineEmits<{
  export: [];
  "history-toggle": [];
  "history-select": [id: string];
  "history-delete": [id: string];
  "new-session": [];
  stop: [];
  submit: [task: string, file?: File];
  "quick-task": [task: string];
  logout: [];
}>();

const menuOpen = ref(false);

function handleLogout() {
  menuOpen.value = false;
  emit("logout");
}
</script>

<style scoped>
.user-menu {
  position: relative;
  display: flex;
  align-items: center;
  gap: 4px;
  cursor: pointer;
  padding: 6px 12px;
  border-radius: 6px;
  transition: background 150ms;
  margin-left: 8px;
}

.user-menu:hover {
  background: var(--paper-high);
}

.user-name {
  font-size: 16px;
  color: var(--ink);
  font-weight: 500;
}

.user-arrow {
  font-size: 12px;
  color: var(--ink-faint);
}

.user-dropdown {
  position: absolute;
  top: 100%;
  right: 0;
  margin-top: 6px;
  min-width: 160px;
  background: var(--paper);
  border: 1px solid #e0dbd0;
  border-radius: 8px;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.08);
  overflow: hidden;
  z-index: 100;
}

.dropdown-item {
  display: block;
  width: 100%;
  padding: 10px 16px;
  font-size: 16px;
  color: var(--ink);
  text-decoration: none;
  text-align: left;
  background: none;
  border: none;
  font-family: "Noto Sans SC", sans-serif;
  cursor: pointer;
  transition: background 150ms;
}

.dropdown-item:hover {
  background: var(--paper-high);
}

.dropdown-divider {
  height: 1px;
  background: #e0dbd0;
  margin: 4px 0;
}

.logout-item {
  color: var(--err);
}
</style>
