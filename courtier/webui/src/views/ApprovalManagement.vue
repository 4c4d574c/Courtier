<template>
  <div class="admin-page">
    <header class="admin-header">
      <div>
        <h1 class="admin-heading">注册审批</h1>
        <p class="admin-subtitle">审批新用户的注册申请</p>
      </div>
    </header>
    <p v-if="actionMsg.text" class="action-msg" :class="actionMsg.ok ? 'msg-ok' : 'msg-err'">
      {{ actionMsg.text }}
    </p>
    <div class="admin-card" v-if="items.length > 0">
      <table class="admin-table">
      <thead>
        <tr>
          <th>用户名</th>
          <th>邮箱</th>
          <th>注册时间</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="item in items" :key="item.id">
          <td class="td-username">{{ item.username }}</td>
          <td class="td-email">{{ item.email || "-" }}</td>
          <td class="td-date">{{ formatDate(item.created_at) }}</td>
          <td class="td-actions">
            <button
              @click="approve(item)"
              class="action-btn approve-btn"
              :disabled="item._loading"
            >
              批准
            </button>
            <button
              @click="reject(item)"
              class="action-btn reject-btn"
              :disabled="item._loading"
            >
              拒绝
            </button>
          </td>
        </tr>
      </tbody>
    </table>
    </div>
    <div v-else-if="!loading" class="admin-card">
      <div class="admin-empty-state">
        <p>暂无待审批的注册申请</p>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted } from "vue";
import { api } from "../api/client";
import { formatDate } from "../utils/date";

interface ApprovalItem {
  id: number;
  username: string;
  email: string;
  created_at: string;
  _loading?: boolean;
}

const items = ref<(ApprovalItem & { _loading?: boolean })[]>([]);
const loading = ref(false);
const actionMsg = reactive({ text: "", ok: false });

function reportError(e: unknown, fallback: string) {
  actionMsg.text = e instanceof Error ? e.message : fallback;
  actionMsg.ok = false;
}

async function approve(item: ApprovalItem & { _loading?: boolean }) {
  item._loading = true;
  try {
    await api.approveUser(item.id);
    items.value = items.value.filter((i) => i.id !== item.id);
    actionMsg.text = `已批准 ${item.username} 的注册申请`;
    actionMsg.ok = true;
  } catch (e: unknown) {
    reportError(e, "批准失败");
  } finally {
    item._loading = false;
  }
}

async function reject(item: ApprovalItem & { _loading?: boolean }) {
  item._loading = true;
  try {
    await api.rejectUser(item.id);
    items.value = items.value.filter((i) => i.id !== item.id);
    actionMsg.text = `已拒绝 ${item.username} 的注册申请`;
    actionMsg.ok = true;
  } catch (e: unknown) {
    reportError(e, "拒绝失败");
  } finally {
    item._loading = false;
  }
}

onMounted(async () => {
  loading.value = true;
  try {
    const res = await api.listApprovals();
    items.value = res.items.map((i) => ({ ...i, _loading: false }));
  } catch (e: unknown) {
    reportError(e, "获取审批列表失败");
  } finally {
    loading.value = false;
  }
});
</script>

<style scoped>
.admin-header {
  margin-bottom: 16px;
}
.action-msg {
  margin: 0 0 12px;
  padding: 10px 14px;
  border-radius: var(--chat-radius-sm);
  font-size: 13px;
}
.msg-ok {
  background: color-mix(in srgb, var(--ok) 10%, transparent);
  color: var(--ok-dim);
}
.msg-err {
  background: color-mix(in srgb, var(--err) 10%, transparent);
  color: var(--err);
}
.approve-btn {
  color: var(--ok);
  border-color: color-mix(in srgb, var(--ok) 40%, transparent);
}
.reject-btn {
  color: var(--err);
  border-color: color-mix(in srgb, var(--err) 40%, transparent);
}
.approve-btn:hover:not(:disabled) {
  background: color-mix(in srgb, var(--ok) 12%, transparent);
}
.reject-btn:hover:not(:disabled) {
  background: color-mix(in srgb, var(--err) 12%, transparent);
}

.admin-empty-state {
  text-align: center;
  padding: 56px 0;
  color: var(--chat-text-tertiary);
  font-size: 13px;
}
</style>
