<template>
  <div :class="['history-panel', { open }]">
    <div v-if="open" class="history-body">
      <div class="history-header">
        <span class="history-title">历史会话</span>
      </div>
      <div
        v-if="loading"
        class="history-list"
        style="text-align: center; color: var(--ink-faint); padding-top: 40px"
      >
        加载中...
      </div>
      <div v-else class="history-list">
        <div
          v-for="s in sessions"
          :key="s.id"
          class="history-item"
          @click="$emit('select', s.id)"
        >
          <span
            class="history-item-delete"
            @click.stop="handleDeleteClick(s.id)"
            title="删除"
            >×</span
          >
          <div class="history-item-task">{{ s.task }}</div>
          <div class="history-item-meta">
            <span>
              <span :class="['history-item-status', s.status]"></span>
              {{ formatStatus(s.status) }}
            </span>
            <span>{{ formatDate(s.createdAt) }}</span>
            <span>{{ s.stepCount }}步 · {{ s.toolCount }}工具</span>
          </div>
        </div>
        <div
          v-if="sessions.length === 0"
          style="text-align: center; color: var(--ink-faint); padding-top: 40px"
        >
          暂无历史会话
        </div>
      </div>
    </div>
    <div
      class="history-flag"
      role="button"
      tabindex="0"
      :aria-expanded="open"
      aria-label="历史会话"
      @click="$emit('toggle')"
      @keydown.enter.prevent="$emit('toggle')"
      @keydown.space.prevent="$emit('toggle')"
      title="历史会话"
    >
      <span class="history-flag-char">历</span>
      <span class="history-flag-diamond">◆</span>
      <span class="history-flag-char">史</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import type { SessionSummary } from "../types/agent";
import { MESSAGES } from "../constants/messages";

interface Props {
  open: boolean;
  sessions: SessionSummary[];
  loading: boolean;
}

defineProps<Props>();
const emit = defineEmits<{
  toggle: [];
  select: [id: string];
  delete: [id: string];
}>();

function formatStatus(status: string): string {
  const map: Record<string, string> = {
    running: "运行中",
    paused: "已暂停",
    completed: "已完成",
    error: "错误",
  };
  return map[status] || status;
}

function handleDeleteClick(id: string) {
  if (window.confirm(MESSAGES.DELETE_CONFIRM)) {
    emit("delete", id);
  }
}

function formatDate(ts: number): string {
  const d = new Date(ts);
  return `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}
</script>
