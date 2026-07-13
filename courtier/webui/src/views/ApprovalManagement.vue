<template>
  <div class="admin-page">
    <h1 class="admin-heading">注册审批</h1>
    <table class="admin-table" v-if="items.length > 0">
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
    <div v-else-if="!loading" class="admin-empty-state">
      <p>暂无待审批的注册申请</p>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from "vue";
import { api } from "../api/client";

interface ApprovalItem {
  id: number;
  username: string;
  email: string;
  created_at: string;
  _loading?: boolean;
}

const items = ref<(ApprovalItem & { _loading?: boolean })[]>([]);
const loading = ref(false);

function formatDate(d: string): string {
  if (!d) return "-";
  return new Date(d).toLocaleDateString("zh-CN");
}

async function approve(item: ApprovalItem & { _loading?: boolean }) {
  item._loading = true;
  try {
    await api.approveUser(item.id);
    items.value = items.value.filter((i) => i.id !== item.id);
  } catch (e: unknown) {
    console.error("Failed to approve:", e);
  } finally {
    item._loading = false;
  }
}

async function reject(item: ApprovalItem & { _loading?: boolean }) {
  item._loading = true;
  try {
    await api.rejectUser(item.id);
    items.value = items.value.filter((i) => i.id !== item.id);
  } catch (e: unknown) {
    console.error("Failed to reject:", e);
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
    console.error("Failed to fetch approvals:", e);
  } finally {
    loading.value = false;
  }
});
</script>

<style scoped>
.approve-btn {
  color: var(--ok);
  border-color: var(--ok);
}
.reject-btn {
  color: var(--err);
  border-color: var(--err);
}
.approve-btn:hover:not(:disabled) {
  background: #e4f4e4;
}
.reject-btn:hover:not(:disabled) {
  background: #fce4e4;
}

.admin-empty-state {
  text-align: center;
  padding: 64px 0;
  color: var(--ink-faint);
  font-size: 17px;
}
</style>
