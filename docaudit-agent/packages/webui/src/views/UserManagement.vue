<template>
  <div class="admin-page">
    <h1 class="admin-heading">用户管理</h1>
    <div class="admin-toolbar">
      <input
        v-model="search"
        type="text"
        placeholder="搜索用户名或邮箱..."
        class="admin-search"
        @input="debouncedSearch"
      />
      <select v-model="roleFilter" @change="fetchUsers" class="admin-select">
        <option value="">全部角色</option>
        <option value="admin">管理员</option>
        <option value="auditor">审计员</option>
      </select>
      <select v-model="statusFilter" @change="fetchUsers" class="admin-select">
        <option value="">全部状态</option>
        <option value="active">活跃</option>
        <option value="disabled">禁用</option>
        <option value="pending">待审批</option>
      </select>
    </div>

    <table class="admin-table">
      <thead>
        <tr>
          <th>用户名</th>
          <th>邮箱</th>
          <th>角色</th>
          <th>状态</th>
          <th>注册时间</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-if="users.length === 0 && !loading">
          <td colspan="6" class="admin-empty">暂无用户数据</td>
        </tr>
        <tr v-for="u in users" :key="u.id">
          <td class="td-username">{{ u.username }}</td>
          <td class="td-email">{{ u.email || "-" }}</td>
          <td>
            <span class="badge" :class="'badge-' + u.role">{{
              u.role === "admin" ? "管理员" : "审计员"
            }}</span>
          </td>
          <td>
            <span class="badge" :class="'badge-' + u.status">{{
              statusLabel(u.status)
            }}</span>
          </td>
          <td class="td-date">{{ formatDate(u.created_at) }}</td>
          <td class="td-actions">
            <select
              :value="u.role"
              @change="
                changeRole(u, ($event.target as HTMLSelectElement).value)
              "
              class="action-select"
            >
              <option value="admin">管理员</option>
              <option value="auditor">审计员</option>
            </select>
            <select
              :value="u.status"
              @change="
                changeStatus(u, ($event.target as HTMLSelectElement).value)
              "
              class="action-select"
            >
              <option value="active">活跃</option>
              <option value="disabled">禁用</option>
            </select>
            <button @click="resetPassword(u)" class="action-btn">
              重置密码
            </button>
          </td>
        </tr>
      </tbody>
    </table>

    <div class="admin-pagination" v-if="total > pageSize">
      <button :disabled="page <= 1" @click="goPage(page - 1)">上一页</button>
      <span>第 {{ page }} / {{ totalPages }} 页（共 {{ total }} 条）</span>
      <button :disabled="page >= totalPages" @click="goPage(page + 1)">
        下一页
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from "vue";
import { api } from "../api/client";

interface AdminUser {
  id: number;
  username: string;
  email: string;
  role: string;
  status: string;
  created_at: string;
}

const users = ref<AdminUser[]>([]);
const search = ref("");
const roleFilter = ref("");
const statusFilter = ref("");
const page = ref(1);
const total = ref(0);
const pageSize = 20;
const loading = ref(false);

const totalPages = computed(() =>
  Math.max(1, Math.ceil(total.value / pageSize)),
);

let searchTimer: ReturnType<typeof setTimeout> | undefined;
function debouncedSearch() {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    page.value = 1;
    fetchUsers();
  }, 300);
}

async function fetchUsers() {
  loading.value = true;
  try {
    const res = await api.listUsers({
      page: page.value,
      page_size: pageSize,
      search: search.value || undefined,
      role: roleFilter.value || undefined,
      status: statusFilter.value || undefined,
    });
    users.value = res.items;
    total.value = res.total;
  } catch (e: unknown) {
    console.error("Failed to fetch users:", e);
  } finally {
    loading.value = false;
  }
}

function goPage(p: number) {
  page.value = p;
  fetchUsers();
}

function statusLabel(s: string): string {
  const map: Record<string, string> = {
    active: "活跃",
    disabled: "禁用",
    pending: "待审批",
  };
  return map[s] || s;
}

function formatDate(d: string): string {
  if (!d) return "-";
  return new Date(d).toLocaleDateString("zh-CN");
}

async function changeRole(u: AdminUser, role: string) {
  try {
    await api.updateUser(u.id, { role });
    u.role = role;
  } catch (e: unknown) {
    console.error("Failed to update role:", e);
  }
}

async function changeStatus(u: AdminUser, status: string) {
  try {
    await api.updateUser(u.id, { status });
    u.status = status;
  } catch (e: unknown) {
    console.error("Failed to update status:", e);
  }
}

async function resetPassword(u: AdminUser) {
  const newPw = prompt(`为 ${u.username} 输入新密码（至少8位）：`);
  if (!newPw) return;
  if (newPw.length < 8) {
    alert("密码至少8位");
    return;
  }
  try {
    await api.updateUser(u.id, { password: newPw });
  } catch (e: unknown) {
    console.error("Failed to reset password:", e);
  }
}

onMounted(fetchUsers);
</script>
